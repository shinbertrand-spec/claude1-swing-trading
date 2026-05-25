"""Tests for tools.thematic_portfolio.kill_switch.monitor — Process B cycle
+ run_forever (dry-run only)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools.broker.tiger import TigerClient
from tools.thematic_portfolio.kill_switch import monitor, state
from tools.thematic_portfolio.kill_switch.clock import SessionState


# --- fake TradeClient -----------------------------------------------------


class FakeTradeClient:
    """Mimics tigeropen TradeClient just enough for TigerClient + monitor.cycle."""

    def __init__(
        self,
        *,
        net_liquidation=1_000_000.0,
        positions: list[dict[str, Any]] | None = None,
        raise_on_positions=False,
    ):
        self.account_full = "PAPER12345"
        self.config_info = {
            "tiger_id_masked": "...1234",
            "account_masked": "...2345",
            "license": "TBSG",
            "is_paper": True,
            "server_url": "https://mock",
            "props_dir": "/mock",
        }
        self._net_liq = net_liquidation
        self._positions = positions or []
        self._raise_on_positions = raise_on_positions
        self.calls: list[tuple] = []
        self.open_orders_to_return: list[Any] = []
        self._next_order_id = 60_000

    def get_assets(self, *, account, segment=False):
        summary = SimpleNamespace(
            cash=self._net_liq * 0.5,
            available_funds=self._net_liq * 0.5,
            buying_power=self._net_liq * 2.0,
            net_liquidation=self._net_liq,
            gross_position_value=self._net_liq * 0.5,
            currency="USD",
        )
        return [SimpleNamespace(summary=summary)]

    def get_positions(self, *, account):
        if self._raise_on_positions:
            raise RuntimeError("BOOM")
        out = []
        for p in self._positions:
            contract = SimpleNamespace(symbol=p["symbol"])
            out.append(SimpleNamespace(
                contract=contract,
                quantity=p["quantity"],
                average_cost=p.get("average_cost", 100.0),
                market_value=p.get("market_value", 0.0),
                unrealized_pnl=p.get("unrealized_pnl", 0.0),
            ))
        return out

    def get_open_orders(self, *, account, **_):
        return list(self.open_orders_to_return)

    def get_contract(self, *, symbol, **_):
        return SimpleNamespace(symbol=symbol, sec_type="STK", currency="USD")

    def place_order(self, order):
        oid = self._next_order_id
        self._next_order_id += 1
        order.id = oid
        self.calls.append((
            "place_order", order.action, order.quantity,
            order.contract.symbol, getattr(order, "limit_price", None),
        ))
        return oid

    def cancel_order(self, *, account, id, **_):
        self.calls.append(("cancel_order", account, id))
        return id


class FakeQuoteClient:
    def __init__(self):
        self.briefs_by_symbol: dict[str, SimpleNamespace] = {}

    def get_briefs(self, *, symbols, include_ask_bid=False, **_):
        return [self.briefs_by_symbol[s] for s in symbols if s in self.briefs_by_symbol]

    def set_quote(self, symbol, bid):
        self.briefs_by_symbol[symbol] = SimpleNamespace(
            symbol=symbol, bid_price=bid, ask_price=bid + 0.05,
            latest_price=bid, bid_size=100, ask_size=100,
            halted=False, delay=0,
        )


def _client(**kw):
    tc = FakeTradeClient(**kw)
    qc = FakeQuoteClient()
    return TigerClient(_trade_client=tc, _quote_client=qc)


def _write_thematic_index(path: Path, tickers: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "1.0",
        "positions": [{"ticker": t, "shares": 100} for t in tickers],
    }), encoding="utf-8")


# --- happy-path single cycle ----------------------------------------------


def test_cycle_with_no_thematic_positions_writes_hold_event(tmp_path):
    index_path = tmp_path / "thematic_index.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, [])

    tiger = _client(positions=[
        # Tiger has AAPL but thematic index is empty -> no thematic positions
        {"symbol": "AAPL", "quantity": 50, "market_value": 8_000.0},
    ])

    result = monitor.cycle(
        tiger=tiger, state_dir=state_dir, index_path=index_path,
        cycle_number=1, dry_run=True,
    )
    assert result.ok is True
    assert result.decision.action == "hold"
    assert result.thematic_market_value == 0.0
    assert result.thematic_symbols == []

    events = state.read_events(state_dir=state_dir)
    assert len(events) == 1
    assert events[0]["action"] == "hold"
    assert events[0]["dry_run"] is True

    hb = state.load_heartbeat(state_dir=state_dir)
    assert hb.cycle_number == 1
    assert hb.last_action == "hold"


def test_cycle_at_peak_no_drawdown(tmp_path):
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, ["NVDA", "BE"])
    tiger = _client(positions=[
        {"symbol": "NVDA", "quantity": 100, "market_value": 150_000.0,
         "average_cost": 1000.0},
        {"symbol": "BE",   "quantity": 1000, "market_value": 100_000.0,
         "average_cost": 80.0},
    ])
    result = monitor.cycle(
        tiger=tiger, state_dir=state_dir, index_path=index_path,
        cycle_number=1, dry_run=True,
    )
    assert result.ok is True
    assert result.thematic_market_value == 250_000.0
    assert result.peak_thematic_value == 250_000.0
    assert result.decision.action == "hold"


# --- tier transitions in the loop -----------------------------------------


def test_two_cycles_peak_then_drawdown_fires_tier1(tmp_path):
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, ["NVDA"])

    # Cycle 1: at peak, 25% allocation, no drawdown
    tiger_peak = _client(positions=[
        {"symbol": "NVDA", "quantity": 1000, "market_value": 250_000.0},
    ])
    r1 = monitor.cycle(
        tiger=tiger_peak, state_dir=state_dir, index_path=index_path,
        cycle_number=1, dry_run=True,
    )
    assert r1.decision.action == "hold"
    assert r1.peak_thematic_value == 250_000.0

    # Cycle 2: -20% drawdown, 20% allocation
    tiger_dd = _client(positions=[
        {"symbol": "NVDA", "quantity": 1000, "market_value": 200_000.0},
    ])
    r2 = monitor.cycle(
        tiger=tiger_dd, state_dir=state_dir, index_path=index_path,
        cycle_number=2, dry_run=True,
    )
    assert r2.decision.action == "deleverage"
    assert r2.decision.tier == 1
    assert r2.peak_thematic_value == 250_000.0  # peak preserved

    events = state.read_events(state_dir=state_dir)
    assert len(events) == 2
    assert events[1]["tier"] == 1
    # Dry-run warning surfaced
    assert any("DRY-RUN" in w for w in events[1]["warnings"])


def test_aschenbrenner_kill_event_flag_fires_tier3_unwind(tmp_path):
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, ["NVDA"])

    # Set the flag externally
    state.set_kill_event(
        signal_type="thesis_abandonment",
        matched_phrase="we exited",
        state_dir=state_dir,
    )

    tiger = _client(positions=[
        {"symbol": "NVDA", "quantity": 1000, "market_value": 250_000.0},
    ])
    r = monitor.cycle(
        tiger=tiger, state_dir=state_dir, index_path=index_path,
        cycle_number=1, dry_run=True,
    )
    assert r.aschenbrenner_kill_event is True
    assert r.decision.action == "unwind"
    assert r.decision.tier == 3
    assert r.decision.aschenbrenner_override is True


# --- dry-run gate ---------------------------------------------------------


def test_no_dry_run_with_hold_decision_does_not_place_orders(tmp_path):
    # Hold decisions should be safe to invoke under no-dry-run.
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, ["NVDA"])
    tiger = _client(positions=[
        {"symbol": "NVDA", "quantity": 1000, "market_value": 250_000.0},
    ])
    r = monitor.cycle(
        tiger=tiger, state_dir=state_dir, index_path=index_path,
        cycle_number=1, dry_run=False,
    )
    assert r.decision.action == "hold"
    events = state.read_events(state_dir=state_dir)
    assert events[0]["dry_run"] is False
    assert events[0]["orders_placed"] == []


def test_no_dry_run_tier3_places_real_orders_end_to_end(tmp_path):
    """End-to-end wiring: kill-event flag set, cycle runs without dry-run,
    exits.execute_kill_switch_sells fires, event log captures placed orders."""
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, ["NVDA", "BE"])

    state.set_kill_event(
        signal_type="thesis_abandonment",
        matched_phrase="we exited",
        state_dir=state_dir,
    )

    tiger = _client(positions=[
        {"symbol": "NVDA", "quantity": 100, "market_value": 80_000.0},
        {"symbol": "BE",   "quantity": 500, "market_value": 20_000.0},
    ])
    tiger._qc.set_quote("NVDA", bid=800.0)
    tiger._qc.set_quote("BE", bid=40.0)

    r = monitor.cycle(
        tiger=tiger, state_dir=state_dir, index_path=index_path,
        cycle_number=1, dry_run=False,
    )
    assert r.decision.action == "unwind"
    assert r.decision.tier == 3

    events = state.read_events(state_dir=state_dir)
    placed = events[0]["orders_placed"]
    # Both NVDA + BE placed
    assert len(placed) == 2
    by_sym = {p["symbol"]: p for p in placed}
    assert by_sym["NVDA"]["action"] == "placed"
    assert by_sym["NVDA"]["shares_to_sell"] == 100
    assert by_sym["NVDA"]["limit_price"] == round(800.0 * 0.999, 2)
    assert by_sym["BE"]["action"] == "placed"
    assert by_sym["BE"]["shares_to_sell"] == 500

    # The broker actually saw the place_order calls
    place_calls = [c for c in tiger._tc.calls if c[0] == "place_order"]
    assert len(place_calls) == 2


def test_no_dry_run_tier1_partial_sell_floor_rounded(tmp_path):
    """Tier-1 sell_fraction = 0.125; 100 shares -> floor(12.5) = 12 shares."""
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, ["NVDA"])

    # Cycle 1: at peak, no DD
    tiger_peak = _client(positions=[
        {"symbol": "NVDA", "quantity": 100, "market_value": 250_000.0},
    ])
    tiger_peak._qc.set_quote("NVDA", bid=2500.0)
    monitor.cycle(
        tiger=tiger_peak, state_dir=state_dir, index_path=index_path,
        cycle_number=1, dry_run=False,
    )

    # Cycle 2: -20% DD = tier 1, allocation 20% > 17.5%, sell_fraction = 0.125
    tiger_dd = _client(positions=[
        {"symbol": "NVDA", "quantity": 100, "market_value": 200_000.0},
    ])
    tiger_dd._qc.set_quote("NVDA", bid=2000.0)
    r2 = monitor.cycle(
        tiger=tiger_dd, state_dir=state_dir, index_path=index_path,
        cycle_number=2, dry_run=False,
    )
    assert r2.decision.tier == 1
    assert r2.decision.sell_fraction == pytest.approx(0.125, abs=1e-6)

    events = state.read_events(state_dir=state_dir)
    placed = events[1]["orders_placed"]
    assert len(placed) == 1
    assert placed[0]["shares_to_sell"] == 12  # floor(100 * 0.125)
    assert placed[0]["action"] == "placed"


# --- error paths ---------------------------------------------------------


def test_cycle_returns_error_on_tiger_read_failure(tmp_path):
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, ["NVDA"])
    tiger = _client(raise_on_positions=True)
    r = monitor.cycle(
        tiger=tiger, state_dir=state_dir, index_path=index_path,
        cycle_number=1, dry_run=True,
    )
    assert r.ok is False
    assert "tiger_read_failed" in r.error
    # No event log entry because we bailed before write
    assert state.read_events(state_dir=state_dir) == []


# --- run_forever ---------------------------------------------------------


def test_run_forever_max_cycles_exits_after_n(tmp_path):
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, [])
    tiger = _client(positions=[])

    sleeps: list[int] = []
    def fake_sleep(s):
        sleeps.append(s)

    fake_clock_state = SessionState(
        is_rth_open=True,
        reason="open",
        now_et_iso="2026-05-26T10:00:00-04:00",
        suggested_sleep_seconds=60,
    )
    n = monitor.run_forever(
        tiger=tiger, state_dir=state_dir, index_path=index_path,
        dry_run=True, max_cycles=3,
        sleep_fn=fake_sleep, clock_fn=lambda: fake_clock_state,
    )
    assert n == 3
    # 3 cycles -> 3 sleeps (1 per cycle)
    assert len(sleeps) == 3
    assert all(s == 60 for s in sleeps)
    events = state.read_events(state_dir=state_dir)
    assert len(events) == 3


def test_run_forever_uses_off_hours_cadence_after_error(tmp_path):
    index_path = tmp_path / "idx.json"
    state_dir = tmp_path / "_state"
    _write_thematic_index(index_path, ["NVDA"])
    tiger = _client(raise_on_positions=True)

    sleeps: list[int] = []
    monitor.run_forever(
        tiger=tiger, state_dir=state_dir, index_path=index_path,
        dry_run=True, max_cycles=2,
        sleep_fn=lambda s: sleeps.append(s),
        clock_fn=lambda: SessionState(True, "open", "x", 60),
    )
    # Both cycles errored at Tiger read -> 300s sleep used both times
    assert sleeps == [300, 300]
