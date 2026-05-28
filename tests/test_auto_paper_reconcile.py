"""Tests for tools.auto_paper.reconcile — EOD fill reconciliation with mocked broker.

Includes Session 3 coverage: on submitted->starter transition, a STP SELL
order is placed via TigerClient.place_stop_loss and the broker order id
is recorded on position_state.stop_order_id. Partial fills size the stop
to filled_qty, not requested.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from tools.auto_paper import reconcile, state
from tools.auto_paper.reconcile import ReconcileResult, reconcile_today


# ------------------------------------------------------- fakes


class FakeTradeClient:
    """Minimal TradeClient stand-in scoped to what reconcile uses.

    Tracks all place_order / cancel_order / get_filled_orders /
    get_open_orders calls so Session 3's stop-placement assertions can
    verify the wiring end-to-end.
    """

    def __init__(self, *, filled=None, open_=None):
        self.account_full = "PAPER87654321"
        self.config_info = {
            "tiger_id_masked": "...5678",
            "account_masked": "...4321",
            "license": "TBSG",
            "is_paper": True,
            "server_url": "https://mock",
            "props_dir": "/mock",
        }
        self._filled = filled or []
        self._open = open_ or []
        self.calls: list[tuple] = []
        self._next_order_id = 70_000

    def get_filled_orders(self, *, account, start_time=None, end_time=None,
                          symbol=None, **_):
        self.calls.append(("get_filled_orders", account, start_time))
        return self._filled

    def get_open_orders(self, *, account, **_):
        self.calls.append(("get_open_orders", account))
        return self._open

    def get_contract(self, *, symbol, **_):
        self.calls.append(("get_contract", symbol))
        return SimpleNamespace(symbol=symbol, sec_type="STK", currency="USD")

    def place_order(self, order):
        oid = self._next_order_id
        self._next_order_id += 1
        order.id = oid
        # Capture (action, qty, symbol, aux_price-or-limit) for assertions.
        aux = getattr(order, "aux_price", None)
        limit = getattr(order, "limit_price", None)
        self.calls.append((
            "place_order", order.action, order.quantity, order.contract.symbol,
            aux, limit,
        ))
        return oid

    def cancel_order(self, *, account, id, **_):
        self.calls.append(("cancel_order", account, id))
        return id


def _fake_order(*, order_id, symbol, filled, avg_fill_price, requested=10):
    return SimpleNamespace(
        id=order_id,
        contract=SimpleNamespace(symbol=symbol),
        action="BUY",
        order_type="LMT",
        quantity=requested,
        filled=filled,
        avg_fill_price=avg_fill_price,
        limit_price=avg_fill_price + 0.10,
        status="Filled" if filled == requested else "PartiallyFilled",
        trade_time="2026-05-24T13:45:12Z",
    )


def _fake_open(*, order_id, symbol, requested=10, limit_price=100.0):
    return SimpleNamespace(
        id=order_id,
        contract=SimpleNamespace(symbol=symbol),
        action="BUY",
        order_type="LMT",
        quantity=requested,
        limit_price=limit_price,
        status="Submitted",
    )


# ------------------------------------------------------- fixtures


@pytest.fixture
def paper_dirs(tmp_path, monkeypatch):
    ledger_dir = tmp_path / "ledgers" / "paper-auto"
    positions_json = tmp_path / "journal" / "paper-auto" / "positions.json"
    monkeypatch.setattr(state, "PAPER_AUTO_LEDGER_DIR", str(ledger_dir))
    monkeypatch.setattr(state, "PAPER_AUTO_POSITIONS_JSON", str(positions_json))
    return ledger_dir, positions_json


def _seed_submitted(paper_dirs, *, ticker, order_id, shares=10, limit_price=850.50,
                    stop_price=820.00):
    """Helper: write a paper-auto ledger in submitted state + append positions.json entry."""
    state.write_submitted_ledger(
        ticker=ticker, setup_type="EP", setup_grade="Swan",
        pivot_price=850.00, limit_price=limit_price, stop_price=stop_price,
        shares=shares, broker_order_id=order_id, broker="tiger_paper",
        sector_etf="XLK",
    )
    state.append_to_positions_json({
        "ticker": ticker.upper(),
        "ledger_path": state.ledger_path(ticker).replace("\\", "/"),
        "entry_date": "2026-05-24",
        "entry_price": limit_price,
        "shares": shares,
        "stop": stop_price,
        "target_1": 910.00,
        "sector": "XLK",
        "broker_order_id": order_id,
        "broker": "tiger_paper",
        "stage": "submitted",
        "setup_type": "EP",
        "setup_grade": "Swan",
    })


def _client(filled=None, open_=None):
    from tools.broker.tiger import TigerClient
    return TigerClient(_trade_client=FakeTradeClient(filled=filled or [], open_=open_ or []))


# ------------------------------------------------------- tests


def test_reconcile_returns_empty_when_nothing_pending(paper_dirs):
    results = reconcile_today(client=_client(), dry_run=True)
    assert results == []


def test_reconcile_filled_full(paper_dirs):
    _seed_submitted(paper_dirs, ticker="NVDA", order_id=10001, shares=10)
    client = _client(filled=[
        _fake_order(order_id=10001, symbol="NVDA", filled=10, avg_fill_price=850.42),
    ])

    results = reconcile_today(client=client, dry_run=False)
    assert len(results) == 1
    r = results[0]
    assert r.ticker == "NVDA"
    assert r.action == "filled"
    assert r.filled_qty == 10
    assert r.requested_qty == 10
    assert r.avg_fill_price == 850.42

    # Ledger updated: state=starter, fill_price=avg_fill
    doc = yaml.safe_load(open(state.ledger_path("NVDA")))
    assert doc["meta"]["state"] == "starter"
    assert doc["meta"]["updated_by"] == "auto_paper/reconcile"
    assert doc["position_state"]["starter"]["fill_price"] == 850.42

    # positions.json updated
    data = json.load(open(state.PAPER_AUTO_POSITIONS_JSON))
    entry = data["positions"][0]
    assert entry["stage"] == "starter"
    assert entry["entry_price"] == 850.42


def test_reconcile_filled_places_broker_stop(paper_dirs):
    """Session 3 — full fill triggers a STP SELL at stop_price for filled_qty."""
    _seed_submitted(paper_dirs, ticker="NVDA", order_id=10001, shares=10, stop_price=820.00)
    client = _client(filled=[
        _fake_order(order_id=10001, symbol="NVDA", filled=10, avg_fill_price=850.42),
    ])

    results = reconcile_today(client=client, dry_run=False)
    r = results[0]
    assert r.action == "filled"
    assert r.stop_order_id is not None
    assert r.stop_place_error is None

    # Inspect broker calls — exactly one place_order for the STP SELL.
    place_calls = [c for c in client._tc.calls if c[0] == "place_order"]
    assert len(place_calls) == 1
    _tag, action, qty, symbol, aux_price, limit = place_calls[0]
    assert action == "SELL"
    assert qty == 10
    assert symbol == "NVDA"
    assert aux_price == 820.00         # stop_price
    assert limit is None               # STP, not STP-LMT

    # Ledger now has stop_order_id recorded.
    doc = yaml.safe_load(open(state.ledger_path("NVDA")))
    assert doc["position_state"]["stop_order_id"] == r.stop_order_id


def test_reconcile_partial_sizes_stop_to_filled_qty(paper_dirs):
    """Session 3 — partial fill sizes the stop to filled_qty, not requested."""
    _seed_submitted(paper_dirs, ticker="AAPL", order_id=10002, shares=15, stop_price=174.00)
    client = _client(filled=[
        _fake_order(order_id=10002, symbol="AAPL", filled=8, avg_fill_price=180.55, requested=15),
    ])

    results = reconcile_today(client=client, dry_run=False)
    r = results[0]
    assert r.action == "partial"
    assert r.filled_qty == 8
    assert r.stop_order_id is not None
    assert r.stop_place_error is None

    # Ledger now sized to 8 shares + stop_order_id recorded.
    doc = yaml.safe_load(open(state.ledger_path("AAPL")))
    starter = doc["position_state"]["starter"]
    assert starter["shares"] == 8
    assert starter["fill_price"] == 180.55
    assert doc["position_state"]["intended_full_shares"] == 8
    assert doc["meta"]["state"] == "starter"
    assert doc["position_state"]["stop_order_id"] == r.stop_order_id

    # Broker STP placed with qty=8 (filled_qty), NOT 15 (requested).
    place_calls = [c for c in client._tc.calls if c[0] == "place_order"]
    assert len(place_calls) == 1
    _tag, action, qty, symbol, aux_price, _limit = place_calls[0]
    assert action == "SELL"
    assert qty == 8
    assert symbol == "AAPL"
    assert aux_price == 174.00


def test_reconcile_partial(paper_dirs):
    _seed_submitted(paper_dirs, ticker="AAPL", order_id=10002, shares=15)
    client = _client(filled=[
        _fake_order(order_id=10002, symbol="AAPL", filled=8, avg_fill_price=180.55, requested=15),
    ])

    results = reconcile_today(client=client, dry_run=False)
    assert results[0].action == "partial"
    assert results[0].filled_qty == 8
    assert results[0].requested_qty == 15
    assert results[0].avg_fill_price == 180.55

    doc = yaml.safe_load(open(state.ledger_path("AAPL")))
    starter = doc["position_state"]["starter"]
    assert starter["shares"] == 8           # shrunk to filled
    assert starter["fill_price"] == 180.55
    assert doc["position_state"]["intended_full_shares"] == 8
    assert doc["meta"]["state"] == "starter"

    data = json.load(open(state.PAPER_AUTO_POSITIONS_JSON))
    assert data["positions"][0]["shares"] == 8


def test_reconcile_still_open(paper_dirs):
    _seed_submitted(paper_dirs, ticker="MSFT", order_id=10003)
    client = _client(
        filled=[],
        open_=[_fake_open(order_id=10003, symbol="MSFT")],
    )

    results = reconcile_today(client=client, dry_run=False)
    assert results[0].action == "still_open"

    # State unchanged
    doc = yaml.safe_load(open(state.ledger_path("MSFT")))
    assert doc["meta"]["state"] == "submitted"  # still pending

    # No broker stop placed for still-open positions.
    place_calls = [c for c in client._tc.calls if c[0] == "place_order"]
    assert place_calls == []


def test_reconcile_expired(paper_dirs):
    _seed_submitted(paper_dirs, ticker="TSLA", order_id=10004)
    client = _client(filled=[], open_=[])

    results = reconcile_today(client=client, dry_run=False)
    assert results[0].action == "expired"

    doc = yaml.safe_load(open(state.ledger_path("TSLA")))
    assert doc["meta"]["state"] == "closed"
    assert "expired unfilled" in doc.get("notes", "")

    data = json.load(open(state.PAPER_AUTO_POSITIONS_JSON))
    assert data["positions"][0]["stage"] == "closed_unfilled"

    # No broker stop placed for expired positions.
    place_calls = [c for c in client._tc.calls if c[0] == "place_order"]
    assert place_calls == []


def test_dry_run_does_not_mutate(paper_dirs):
    _seed_submitted(paper_dirs, ticker="NVDA", order_id=10001, shares=10)
    client = _client(filled=[
        _fake_order(order_id=10001, symbol="NVDA", filled=10, avg_fill_price=850.42),
    ])

    results = reconcile_today(client=client, dry_run=True)
    assert results[0].action == "filled"
    # No stop placed in dry-run mode.
    assert results[0].stop_order_id is None
    assert [c for c in client._tc.calls if c[0] == "place_order"] == []

    # Ledger UNCHANGED: still submitted, fill_price still equal to limit
    doc = yaml.safe_load(open(state.ledger_path("NVDA")))
    assert doc["meta"]["state"] == "submitted"
    assert doc["position_state"]["starter"]["fill_price"] == 850.50  # original limit
    assert "stop_order_id" not in doc.get("position_state", {})


def test_reconcile_multiple_mixed(paper_dirs):
    _seed_submitted(paper_dirs, ticker="NVDA", order_id=10001, shares=10)
    _seed_submitted(paper_dirs, ticker="AAPL", order_id=10002, shares=15)
    _seed_submitted(paper_dirs, ticker="MSFT", order_id=10003, shares=5)
    _seed_submitted(paper_dirs, ticker="TSLA", order_id=10004, shares=8)

    client = _client(
        filled=[
            _fake_order(order_id=10001, symbol="NVDA", filled=10, avg_fill_price=850.42),
            _fake_order(order_id=10002, symbol="AAPL", filled=8, avg_fill_price=180.55, requested=15),
        ],
        open_=[
            _fake_open(order_id=10003, symbol="MSFT"),
        ],
        # TSLA absent → expired
    )

    results = reconcile_today(client=client, dry_run=False)
    by_ticker = {r.ticker: r for r in results}
    assert by_ticker["NVDA"].action == "filled"
    assert by_ticker["AAPL"].action == "partial"
    assert by_ticker["MSFT"].action == "still_open"
    assert by_ticker["TSLA"].action == "expired"

    # Exactly two broker stops placed — one for each filled / partial.
    place_calls = [c for c in client._tc.calls if c[0] == "place_order"]
    assert len(place_calls) == 2


def test_reconcile_broker_fetch_error(paper_dirs):
    _seed_submitted(paper_dirs, ticker="NVDA", order_id=10001)

    class _BoomTC(FakeTradeClient):
        def get_filled_orders(self, *a, **kw):
            raise RuntimeError("HTTP 503")

    from tools.broker.tiger import TigerClient
    client = TigerClient(_trade_client=_BoomTC())

    results = reconcile_today(client=client, dry_run=False)
    assert all(r.action == "error" for r in results)
    assert "HTTP 503" in results[0].reason


def test_reconcile_filled_missing_avg_fill_marks_error(paper_dirs):
    _seed_submitted(paper_dirs, ticker="NVDA", order_id=10001, shares=10)
    # Broker returns a filled order with missing avg_fill_price (data quality)
    broken = SimpleNamespace(
        id=10001,
        contract=SimpleNamespace(symbol="NVDA"),
        action="BUY", order_type="LMT",
        quantity=10, filled=10,
        avg_fill_price=None,
        limit_price=850.50, status="Filled",
        trade_time=None,
    )
    client = _client(filled=[broken])
    results = reconcile_today(client=client, dry_run=False)
    assert results[0].action == "error"
    assert "missing qty/avg_fill" in results[0].reason


def test_stop_placement_failure_is_non_fatal(paper_dirs):
    """Session 3 — broker stop placement failure must NOT roll back the fill.

    The ledger should transition to starter, the result should carry the
    error in stop_place_error, but the state transition stays committed
    (so the next monitor run can attempt to re-place the stop).
    """
    _seed_submitted(paper_dirs, ticker="NVDA", order_id=10001, shares=10, stop_price=820.00)

    class _StopBoomTC(FakeTradeClient):
        def place_order(self, order):
            # Fail on the stop placement (SELL side); allow buy if we had one.
            if order.action == "SELL":
                raise RuntimeError("STOP_REJECTED_NOT_TRADING_HOURS")
            return super().place_order(order)

    from tools.broker.tiger import TigerClient
    client = TigerClient(_trade_client=_StopBoomTC(
        filled=[_fake_order(order_id=10001, symbol="NVDA", filled=10, avg_fill_price=850.42)],
    ))

    results = reconcile_today(client=client, dry_run=False)
    r = results[0]
    assert r.action == "filled"
    assert r.stop_order_id is None
    assert "STOP_REJECTED" in (r.stop_place_error or "")

    # Ledger still transitioned to starter; stop_order_id absent.
    doc = yaml.safe_load(open(state.ledger_path("NVDA")))
    assert doc["meta"]["state"] == "starter"
    assert "stop_order_id" not in doc.get("position_state", {})


# ------------------------------------------------------- refresh_starter_stops (2026-05-28)
#
# Tiger paper STP SELL orders are DAY-only — they auto-cancel at session
# close. Before today, the reconcile path placed stops only on the
# submitted→starter transition (once per position's lifetime), so DAY-
# expired stops never got re-armed. Today's smoke test caught this with
# MXL / GO / VRT all carrying stale stop_order_ids but no live broker
# orders. refresh_starter_stops sweeps starter positions, detects
# missing-at-broker stops, and re-places them.


def _seed_starter(paper_dirs, *, ticker, shares, stop_price, stop_order_id):
    """Seed a starter-state ledger + positions.json entry with the given stop_order_id."""
    state.write_submitted_ledger(
        ticker=ticker, setup_type="EP", setup_grade="Swan",
        pivot_price=850.00, limit_price=850.50, stop_price=stop_price,
        shares=shares, broker_order_id=99001, broker="tiger_paper",
        sector_etf="XLK",
    )
    # Flip ledger from submitted → starter manually + record stop_order_id.
    doc = yaml.safe_load(open(state.ledger_path(ticker)))
    doc["meta"]["state"] = "starter"
    doc["position_state"]["stop_order_id"] = stop_order_id
    with open(state.ledger_path(ticker), "w") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)
    # positions.json entry with stage=starter (mirrors post-reconcile state).
    state.append_to_positions_json({
        "ticker": ticker.upper(),
        "ledger_path": state.ledger_path(ticker).replace("\\", "/"),
        "entry_date": "2026-05-24",
        "entry_price": 850.42, "shares": shares, "stop": stop_price,
        "target_1": 910.00, "sector": "XLK",
        "broker_order_id": 99001, "broker": "tiger_paper",
        "stage": "starter",
        "setup_type": "EP", "setup_grade": "Swan",
    })


def test_refresh_skips_when_no_starter_positions(paper_dirs):
    """Empty starters list → empty results, no broker calls."""
    client = _client()
    results = reconcile.refresh_starter_stops(client=client)
    assert results == []


def test_refresh_reports_intact_when_stop_open_at_broker(paper_dirs):
    """Stop_order_id from ledger is in broker open_orders → stop_intact, no replace."""
    _seed_starter(paper_dirs, ticker="NVDA", shares=10, stop_price=820.00,
                  stop_order_id=55001)
    # Broker has the matching STP order open.
    open_stp = SimpleNamespace(
        id=55001, contract=SimpleNamespace(symbol="NVDA"),
        action="SELL", order_type="STP",
        quantity=10, limit_price=None, status="Submitted",
    )
    client = _client(open_=[open_stp])
    results = reconcile.refresh_starter_stops(client=client)
    assert len(results) == 1
    assert results[0].action == "stop_intact"
    assert results[0].stop_order_id == 55001


def test_refresh_replaces_when_stop_expired_at_broker(paper_dirs):
    """Stop_order_id from ledger NOT in broker open_orders → place fresh, update ledger."""
    _seed_starter(paper_dirs, ticker="MXL", shares=503, stop_price=65.77,
                  stop_order_id=44001)
    # Broker has NO open orders → stop expired.
    client = _client(open_=[])
    results = reconcile.refresh_starter_stops(client=client)
    assert len(results) == 1
    r = results[0]
    assert r.action == "stop_replaced"
    assert r.stop_order_id is not None and r.stop_order_id != 44001
    # Ledger updated with the new id.
    doc = yaml.safe_load(open(state.ledger_path("MXL")))
    assert doc["position_state"]["stop_order_id"] == r.stop_order_id


def test_refresh_detects_live_stop_by_symbol_when_ledger_id_stale(paper_dirs):
    """If the broker has ANY STP SELL on the symbol, treat as protected
    even when the ledger's recorded stop_order_id is different (e.g. id
    was rotated externally). Avoids double-stopping."""
    _seed_starter(paper_dirs, ticker="VRT", shares=152, stop_price=289.66,
                  stop_order_id=11111)  # stale id
    # Broker has a different-id STP for VRT (recently re-placed elsewhere).
    open_stp = SimpleNamespace(
        id=99999, contract=SimpleNamespace(symbol="VRT"),
        action="SELL", order_type="STP",
        quantity=152, limit_price=None, status="Submitted",
    )
    client = _client(open_=[open_stp])
    results = reconcile.refresh_starter_stops(client=client)
    assert results[0].action == "stop_intact", (
        "symbol-match should count as live protection, not trigger double-stop"
    )


def test_refresh_dry_run_does_not_place(paper_dirs):
    """dry_run=True surfaces would-place intent without calling the broker."""
    _seed_starter(paper_dirs, ticker="GO", shares=3113, stop_price=6.41,
                  stop_order_id=22222)
    client = _client(open_=[])
    results = reconcile.refresh_starter_stops(client=client, dry_run=True)
    assert results[0].action == "stop_dry_run"
    assert "would place STP" in (results[0].reason or "")
    # Ledger's stop_order_id unchanged.
    doc = yaml.safe_load(open(state.ledger_path("GO")))
    assert doc["position_state"]["stop_order_id"] == 22222


def test_reconcile_today_invokes_refresh_for_starters(paper_dirs):
    """End-to-end: reconcile_today now refreshes starter stops alongside
    processing submitted fills. Mixed payload: one starter with expired stop
    + one submitted fill → results include both refresh and fill actions."""
    _seed_starter(paper_dirs, ticker="MXL", shares=503, stop_price=65.77,
                  stop_order_id=44001)
    _seed_submitted(paper_dirs, ticker="NVDA", order_id=10001, shares=10,
                    stop_price=820.00)
    client = _client(
        filled=[_fake_order(order_id=10001, symbol="NVDA", filled=10,
                            avg_fill_price=850.42)],
        open_=[],  # MXL stop expired; NVDA submitted not yet filled-and-open
    )
    results = reconcile_today(client=client, dry_run=False)
    actions = {r.ticker: r.action for r in results}
    assert actions.get("MXL") == "stop_replaced", f"MXL stop should refresh; got {actions}"
    assert actions.get("NVDA") == "filled", f"NVDA submitted should fill; got {actions}"
