"""Tests for tools.thematic_portfolio.kill_switch.exits — Process B's
order-placement arm.

Covers:
  * tier-1 partial sell (sell_fraction < 1.0): floor-rounded quantity,
    correct limit price = bid * 0.999, broker order placed
  * tier-3 full unwind (sell_fraction = 1.0): sell full quantity
  * cancel-then-place: pre-existing non-SELL orders (STP stops) cancelled
    before the limit-sell is placed
  * existing-pending-sell idempotency: a pending SELL on the symbol skips
    rather than double-placing
  * UNPROTECTED-state recovery: cancel succeeds, place fails -> action is
    "unprotected_after_place_fail" and symbol appears in unprotected list
  * UNPROTECTED-state recovery: cancel succeeds, get_quote fails -> same
  * per-position errors do NOT abort the rest of the loop
  * fractional-share / zero-fraction edge cases

No live broker calls; everything injected via FakeTradeClient + FakeQuoteClient.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.broker.tiger import TigerClient
from tools.thematic_portfolio.kill_switch.exits import (
    LIMIT_SELL_SLIPPAGE,
    execute_kill_switch_sells,
    _compute_shares_to_sell,
)
from tools.thematic_portfolio.kill_switch.ladder import KillSwitchDecision
from tools.thematic_portfolio.kill_switch.positions import ThematicPosition


# --- fakes ----------------------------------------------------------------


class _FakeBrief(SimpleNamespace):
    pass


class _FakeContract(SimpleNamespace):
    pass


class _FakeOrder(SimpleNamespace):
    pass


class FakeQuoteClient:
    def __init__(self):
        self.briefs_by_symbol: dict[str, _FakeBrief] = {}
        self.raise_on_call = False
        self.symbol_specific_raise: set[str] = set()
        self.calls: list[tuple] = []

    def get_briefs(self, *, symbols, include_ask_bid=False, **_):
        self.calls.append(("get_briefs", tuple(symbols), include_ask_bid))
        if self.raise_on_call:
            raise RuntimeError("QUOTE_API_ERROR")
        for s in symbols:
            if s in self.symbol_specific_raise:
                raise RuntimeError(f"QUOTE_API_ERROR_{s}")
        return [
            self.briefs_by_symbol[s] for s in symbols
            if s in self.briefs_by_symbol
        ]


class FakeTradeClient:
    def __init__(self):
        self.account_full = "PAPER12345"
        self.config_info = {
            "tiger_id_masked": "...1234",
            "account_masked": "...2345",
            "license": "TBSG",
            "is_paper": True,
            "server_url": "https://mock",
            "props_dir": "/mock",
        }
        self.calls: list[tuple] = []
        self._next_order_id = 50_000
        self.open_orders_to_return: list[_FakeOrder] = []
        # Per-symbol behavior controls
        self.place_raises_for: set[str] = set()
        self.cancel_raises_for_id: set[int] = set()

    def get_open_orders(self, *, account, **_):
        self.calls.append(("get_open_orders", account))
        return list(self.open_orders_to_return)

    def get_contract(self, *, symbol, **_):
        return _FakeContract(symbol=symbol, sec_type="STK", currency="USD")

    def place_order(self, order):
        if order.contract.symbol in self.place_raises_for:
            raise RuntimeError(f"PLACE_FAILED_{order.contract.symbol}")
        oid = self._next_order_id
        self._next_order_id += 1
        order.id = oid
        self.calls.append((
            "place_order", order.action, order.quantity,
            order.contract.symbol, order.limit_price,
        ))
        return oid

    def cancel_order(self, *, account, id, **_):
        if id in self.cancel_raises_for_id:
            raise RuntimeError(f"CANCEL_FAILED_{id}")
        self.calls.append(("cancel_order", account, id))
        return id


def _client(tc=None, qc=None):
    return TigerClient(_trade_client=tc or FakeTradeClient(), _quote_client=qc or FakeQuoteClient())


def _quote(symbol, bid=100.0, ask=100.10, latest=None):
    return _FakeBrief(
        symbol=symbol, bid_price=bid, ask_price=ask,
        latest_price=latest if latest is not None else (bid + ask) / 2,
        bid_size=100, ask_size=100, halted=False, delay=0,
    )


def _pos(ticker, shares=100, market_value=10_000.0):
    return ThematicPosition(
        ticker=ticker,
        shares=shares,
        market_value=market_value,
        average_cost=market_value / shares if shares else 0.0,
        unrealized_pnl=0.0,
    )


def _decision(sell_fraction, action="deleverage", tier=1):
    return KillSwitchDecision(
        action=action,
        tier=tier,
        drawdown_pct=0.25,
        current_allocation_pct=0.25,
        target_allocation_pct=0.175,
        sell_fraction=sell_fraction,
        rationale="test",
    )


# --- _compute_shares_to_sell -----------------------------------------------


@pytest.mark.parametrize("shares,frac,expected", [
    (1000, 0.30, 300),
    (1000, 0.30 + 1e-12, 300),   # tiny epsilon irrelevant
    (1000, 1.0, 1000),           # tier 3 full unwind
    (100, 0.125, 12),            # floor not 12.5
    (100, 0.50, 50),
    (100, 0.0, 0),
    (100, -0.5, 0),              # negative -> 0
    (0, 0.5, 0),                 # zero shares -> 0
    (1, 0.99, 0),                # 1 share * 0.99 -> floor 0
    (1, 1.0, 1),                 # tier 3 still sells the 1
])
def test_compute_shares_to_sell(shares, frac, expected):
    assert _compute_shares_to_sell(shares, frac) == expected


# --- tier-1 partial sell --------------------------------------------------


def test_tier1_places_floor_rounded_sell_with_minus_0_1_pct_slippage():
    tc = FakeTradeClient()
    qc = FakeQuoteClient()
    qc.briefs_by_symbol["NVDA"] = _quote("NVDA", bid=850.10, ask=850.50)
    tiger = _client(tc, qc)

    positions = [_pos("NVDA", shares=1000, market_value=850_000.0)]
    decision = _decision(sell_fraction=0.125)

    result = execute_kill_switch_sells(
        tiger=tiger, thematic_positions=positions,
        decision=decision, cycle_id="cycle-test-001",
    )

    assert result.n_orders_placed == 1
    r = result.per_symbol_results[0]
    assert r.action == "placed"
    assert r.shares_to_sell == 125  # floor(1000 * 0.125)
    expected_limit = round(850.10 * (1 - LIMIT_SELL_SLIPPAGE), 2)
    assert r.limit_price == expected_limit
    assert r.bid_price == 850.10
    assert r.place_order_id == 50_000

    place_calls = [c for c in tc.calls if c[0] == "place_order"]
    assert len(place_calls) == 1
    _, action, qty, symbol, limit_price = place_calls[0]
    assert action == "SELL"
    assert qty == 125
    assert symbol == "NVDA"
    assert limit_price == expected_limit


# --- tier-3 full unwind ---------------------------------------------------


def test_tier3_sells_full_quantity_across_multiple_positions():
    tc = FakeTradeClient()
    qc = FakeQuoteClient()
    qc.briefs_by_symbol["NVDA"] = _quote("NVDA", bid=850.0, ask=850.5)
    qc.briefs_by_symbol["BE"] = _quote("BE", bid=40.0, ask=40.10)
    tiger = _client(tc, qc)

    positions = [
        _pos("NVDA", shares=500, market_value=425_000.0),
        _pos("BE",   shares=200, market_value=8_000.0),
    ]
    decision = _decision(sell_fraction=1.0, action="unwind", tier=3)

    result = execute_kill_switch_sells(
        tiger=tiger, thematic_positions=positions,
        decision=decision, cycle_id="cycle-test-002",
    )

    assert result.n_orders_placed == 2
    assert {r.symbol for r in result.per_symbol_results} == {"NVDA", "BE"}
    by_symbol = {r.symbol: r for r in result.per_symbol_results}
    assert by_symbol["NVDA"].shares_to_sell == 500
    assert by_symbol["BE"].shares_to_sell == 200
    # All placed
    assert all(r.action == "placed" for r in result.per_symbol_results)


# --- cancel-then-place ----------------------------------------------------


def test_cancels_protective_stp_sell_then_places_limit_sell():
    """A pre-existing STP SELL (protective stop from Process A) gets
    cancelled to make way for the more aggressive kill-switch LMT SELL."""
    tc = FakeTradeClient()
    tc.open_orders_to_return = [
        _FakeOrder(
            contract=_FakeContract(symbol="NVDA"),
            id=99_001, action="SELL", order_type="STP",
            quantity=1000, limit_price=None, status="Submitted",
        )
    ]
    qc = FakeQuoteClient()
    qc.briefs_by_symbol["NVDA"] = _quote("NVDA", bid=800.0, ask=800.5)
    tiger = _client(tc, qc)

    positions = [_pos("NVDA", shares=1000, market_value=800_000.0)]
    result = execute_kill_switch_sells(
        tiger=tiger, thematic_positions=positions,
        decision=_decision(1.0, action="unwind", tier=3),
        cycle_id="cycle-cancel-stp",
    )
    r = result.per_symbol_results[0]
    assert r.action == "placed"
    assert 99_001 in r.cancelled_order_ids
    # Both cancel + place fired
    cancel_calls = [c for c in tc.calls if c[0] == "cancel_order"]
    place_calls = [c for c in tc.calls if c[0] == "place_order"]
    assert len(cancel_calls) == 1
    assert len(place_calls) == 1


def test_pending_lmt_sell_triggers_idempotency_skip():
    """A pending LMT SELL on the same symbol means a prior kill-switch
    cycle already placed — skip rather than stack."""
    tc = FakeTradeClient()
    tc.open_orders_to_return = [
        _FakeOrder(
            contract=_FakeContract(symbol="NVDA"),
            id=99_010, action="SELL", order_type="LMT",
            quantity=1000, limit_price=798.0, status="Submitted",
        )
    ]
    qc = FakeQuoteClient()
    qc.briefs_by_symbol["NVDA"] = _quote("NVDA", bid=800.0, ask=800.5)
    tiger = _client(tc, qc)

    positions = [_pos("NVDA", shares=1000, market_value=800_000.0)]
    result = execute_kill_switch_sells(
        tiger=tiger, thematic_positions=positions,
        decision=_decision(1.0, action="unwind", tier=3),
        cycle_id="cycle-idempotent",
    )
    r = result.per_symbol_results[0]
    assert r.action == "skipped_existing"
    # No place_order calls, no cancel_order calls — idempotent no-op
    assert not any(c[0] == "place_order" for c in tc.calls)
    assert not any(c[0] == "cancel_order" for c in tc.calls)


def test_cancels_non_sell_orders_then_places_limit_sell():
    """Pre-existing non-SELL orders (e.g. a stale BUY) get cancelled."""
    tc = FakeTradeClient()
    # Existing BUY order on the same symbol (not a SELL, so no idempotency hit)
    tc.open_orders_to_return = [
        _FakeOrder(
            contract=_FakeContract(symbol="NVDA"),
            id=99_002, action="BUY", order_type="LMT",
            quantity=100, limit_price=850.0, status="Submitted",
        )
    ]
    qc = FakeQuoteClient()
    qc.briefs_by_symbol["NVDA"] = _quote("NVDA", bid=800.0, ask=800.5)
    tiger = _client(tc, qc)

    positions = [_pos("NVDA", shares=1000, market_value=800_000.0)]
    result = execute_kill_switch_sells(
        tiger=tiger, thematic_positions=positions,
        decision=_decision(1.0, action="unwind", tier=3),
        cycle_id="cycle-cancel-buy",
    )
    r = result.per_symbol_results[0]
    assert r.action == "placed"
    assert 99_002 in r.cancelled_order_ids
    # Both cancel + place fired
    cancel_calls = [c for c in tc.calls if c[0] == "cancel_order"]
    place_calls = [c for c in tc.calls if c[0] == "place_order"]
    assert len(cancel_calls) == 1
    assert len(place_calls) == 1


# --- UNPROTECTED-state recovery -------------------------------------------


def test_cancel_succeeds_place_fails_marks_unprotected():
    tc = FakeTradeClient()
    tc.open_orders_to_return = [
        _FakeOrder(
            contract=_FakeContract(symbol="NVDA"),
            id=99_003, action="BUY", order_type="LMT",
            quantity=100, limit_price=850.0, status="Submitted",
        )
    ]
    tc.place_raises_for = {"NVDA"}
    qc = FakeQuoteClient()
    qc.briefs_by_symbol["NVDA"] = _quote("NVDA", bid=800.0)
    tiger = _client(tc, qc)

    positions = [_pos("NVDA", shares=1000, market_value=800_000.0)]
    result = execute_kill_switch_sells(
        tiger=tiger, thematic_positions=positions,
        decision=_decision(1.0, action="unwind", tier=3),
        cycle_id="cycle-unprot",
    )
    r = result.per_symbol_results[0]
    assert r.action == "unprotected_after_place_fail"
    assert r.cancelled_order_ids == [99_003]
    assert "PLACE_FAILED_NVDA" in r.error
    assert "NVDA" in result.unprotected_symbols
    assert result.n_orders_placed == 0
    assert result.n_symbols_errored == 1


def test_cancel_succeeds_quote_fails_marks_unprotected():
    tc = FakeTradeClient()
    tc.open_orders_to_return = [
        _FakeOrder(
            contract=_FakeContract(symbol="NVDA"),
            id=99_004, action="BUY", order_type="LMT",
            quantity=100, limit_price=850.0, status="Submitted",
        )
    ]
    qc = FakeQuoteClient()
    qc.symbol_specific_raise = {"NVDA"}
    tiger = _client(tc, qc)

    positions = [_pos("NVDA", shares=1000, market_value=800_000.0)]
    result = execute_kill_switch_sells(
        tiger=tiger, thematic_positions=positions,
        decision=_decision(1.0, action="unwind", tier=3),
        cycle_id="cycle-unprot-quote",
    )
    r = result.per_symbol_results[0]
    assert r.action == "unprotected_after_place_fail"
    assert r.cancelled_order_ids == [99_004]
    assert "QUOTE_API_ERROR_NVDA" in r.error
    assert "NVDA" in result.unprotected_symbols


def test_quote_fails_no_cancel_just_error():
    """No pre-existing orders to cancel; quote fails. Not 'unprotected'."""
    tc = FakeTradeClient()
    qc = FakeQuoteClient()
    qc.symbol_specific_raise = {"NVDA"}
    tiger = _client(tc, qc)

    positions = [_pos("NVDA", shares=1000)]
    result = execute_kill_switch_sells(
        tiger=tiger, thematic_positions=positions,
        decision=_decision(1.0, action="unwind", tier=3),
        cycle_id="cycle-quote-fail",
    )
    r = result.per_symbol_results[0]
    assert r.action == "error_quote"
    assert result.unprotected_symbols == []


# --- per-position isolation ----------------------------------------------


def test_one_symbol_error_does_not_abort_others():
    tc = FakeTradeClient()
    tc.place_raises_for = {"NVDA"}  # NVDA fails to place
    qc = FakeQuoteClient()
    qc.briefs_by_symbol["NVDA"] = _quote("NVDA", bid=800.0)
    qc.briefs_by_symbol["BE"] = _quote("BE", bid=40.0)
    tiger = _client(tc, qc)

    positions = [
        _pos("NVDA", shares=1000, market_value=800_000.0),
        _pos("BE",   shares=500,  market_value=20_000.0),
    ]
    result = execute_kill_switch_sells(
        tiger=tiger, thematic_positions=positions,
        decision=_decision(1.0, action="unwind", tier=3),
        cycle_id="cycle-isolation",
    )
    by_sym = {r.symbol: r for r in result.per_symbol_results}
    assert by_sym["NVDA"].action == "error_place"
    assert by_sym["BE"].action == "placed"
    assert result.n_orders_placed == 1
    assert result.n_symbols_errored == 1


# --- edge: bid stale, fall back to latest_price ---------------------------


def test_zero_bid_falls_back_to_latest_price():
    tc = FakeTradeClient()
    qc = FakeQuoteClient()
    qc.briefs_by_symbol["NVDA"] = _quote("NVDA", bid=0.0, ask=0.0, latest=798.50)
    tiger = _client(tc, qc)

    positions = [_pos("NVDA", shares=1000)]
    result = execute_kill_switch_sells(
        tiger=tiger, thematic_positions=positions,
        decision=_decision(1.0, action="unwind", tier=3),
        cycle_id="cycle-stale-bid",
    )
    r = result.per_symbol_results[0]
    assert r.action == "placed"
    # Limit derived from latest_price * 0.999
    assert r.limit_price == round(798.50 * (1 - LIMIT_SELL_SLIPPAGE), 2)


def test_zero_bid_and_zero_latest_errors():
    tc = FakeTradeClient()
    qc = FakeQuoteClient()
    qc.briefs_by_symbol["NVDA"] = _quote("NVDA", bid=0.0, ask=0.0, latest=0.0)
    tiger = _client(tc, qc)

    positions = [_pos("NVDA", shares=1000)]
    result = execute_kill_switch_sells(
        tiger=tiger, thematic_positions=positions,
        decision=_decision(1.0, action="unwind", tier=3),
        cycle_id="cycle-no-price",
    )
    r = result.per_symbol_results[0]
    assert r.action == "error_quote"
    assert "no usable bid" in r.error


# --- zero-shares + empty positions ----------------------------------------


def test_zero_shares_to_sell_is_skipped():
    """Position with 1 share and tier-1 fraction 0.125 -> 0 shares to sell."""
    tc = FakeTradeClient()
    qc = FakeQuoteClient()
    qc.briefs_by_symbol["TINY"] = _quote("TINY", bid=10.0)
    tiger = _client(tc, qc)

    positions = [_pos("TINY", shares=1, market_value=10.0)]
    result = execute_kill_switch_sells(
        tiger=tiger, thematic_positions=positions,
        decision=_decision(0.125),
        cycle_id="cycle-tiny",
    )
    r = result.per_symbol_results[0]
    assert r.action == "skipped_zero_shares"
    assert result.n_orders_placed == 0


def test_empty_positions_returns_empty_result():
    tiger = _client()
    result = execute_kill_switch_sells(
        tiger=tiger, thematic_positions=[],
        decision=_decision(1.0, action="unwind", tier=3),
        cycle_id="cycle-empty",
    )
    assert result.n_orders_placed == 0
    assert result.per_symbol_results == []
    assert result.unprotected_symbols == []
