"""Tests for tools.auto_paper.exits — per-bar sell-decision auto-exit composer.

Covers:
  * no-op when no paper-auto positions are in `starter` state
  * hold action → no place / no cancel / no state change (but sell_eval appended)
  * sell action → places limit-sell + cancels old stop + transitions to closed
  * dry-run path → no broker calls, no ledger mutation for SELL actions
  * mocked OHLCV + mocked sell_decision composition via monkeypatching

Never hits live broker / live OHLCV — everything injected.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import yaml

from tools.auto_paper import cron_gate, exits, state
from tools.auto_paper.exits import ExitResult, evaluate_exits


# ------------------------------------------------------- fakes


class FakeTradeClient:
    """Minimal stand-in: records every place_order / cancel_order call."""

    def __init__(self, *, is_paper=True, place_raises=False, cancel_accepted=True):
        self.account_full = "PAPER87654321"
        self.config_info = {
            "tiger_id_masked": "...5678",
            "account_masked": "...4321",
            "license": "TBSG",
            "is_paper": is_paper,
            "server_url": "https://mock",
            "props_dir": "/mock",
        }
        self.calls: list[tuple] = []
        self._next_order_id = 90_000
        self._place_raises = place_raises
        self._cancel_accepted = cancel_accepted

    def get_contract(self, *, symbol, **_):
        return SimpleNamespace(symbol=symbol, sec_type="STK", currency="USD")

    def place_order(self, order):
        if self._place_raises:
            raise RuntimeError("PLACE_FAILED")
        oid = self._next_order_id
        self._next_order_id += 1
        order.id = oid
        self.calls.append((
            "place_order", order.action, order.quantity, order.contract.symbol,
            getattr(order, "limit_price", None), getattr(order, "aux_price", None),
        ))
        return oid

    def cancel_order(self, *, account, id, **_):
        self.calls.append(("cancel_order", account, id))
        return id if self._cancel_accepted else None


def _client(**kw):
    from tools.broker.tiger import TigerClient
    return TigerClient(_trade_client=FakeTradeClient(**kw))


def _synthetic_ohlcv(n=300, start_close=100.0, drift=0.001, *, parabolic_tail=False):
    """Build a synthetic OHLCV DataFrame with enough history for the detectors.

    base_stage_detect requires PRIOR_HIGH_LOOKBACK + SWING_WINDOW = 262 bars;
    default n=300 keeps it well above.

    parabolic_tail=True bolts a 30% gain over the last 8 bars on top of a
    calm drift so climax-top fires.
    """
    rng = np.random.default_rng(42)
    closes = [start_close]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1.0 + drift + rng.normal(0, 0.005)))
    closes = np.array(closes)

    if parabolic_tail:
        # Boost the last 8 bars by ~30% on rising volume.
        for i in range(-8, 0):
            closes[i] = closes[i] * 1.05

    opens = closes * (1.0 - rng.normal(0, 0.002, size=n))
    highs = np.maximum(opens, closes) * (1.0 + abs(rng.normal(0, 0.003, size=n)))
    lows = np.minimum(opens, closes) * (1.0 - abs(rng.normal(0, 0.003, size=n)))
    volumes = rng.integers(900_000, 1_100_000, size=n).astype(float)
    if parabolic_tail:
        volumes[-1] = volumes.max() * 1.5

    idx = pd.bdate_range(end=pd.Timestamp("2026-05-23"), periods=n)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=idx,
    )


def _fake_fetch(df, ticker_to_df=None):
    """Build a fetch_ohlcv_fn that returns the provided df (or per-ticker dict)."""
    def _f(ticker, period="1y", interval="1d"):
        if ticker_to_df is not None and ticker in ticker_to_df:
            chosen = ticker_to_df[ticker]
        else:
            chosen = df
        return SimpleNamespace(
            df=chosen,
            fetched_at="2026-05-24T00:00:00+00:00",
            source=f"fake:{ticker}",
            ticker=ticker, period=period, interval=interval,
        )
    return _f


# ------------------------------------------------------- fixtures


@pytest.fixture
def paper_dirs(tmp_path, monkeypatch):
    ledger_dir = tmp_path / "ledgers" / "paper-auto"
    positions_json = tmp_path / "journal" / "paper-auto" / "positions.json"
    monkeypatch.setattr(state, "PAPER_AUTO_LEDGER_DIR", str(ledger_dir))
    monkeypatch.setattr(state, "PAPER_AUTO_POSITIONS_JSON", str(positions_json))
    # Hermeticity: isolate the cron-gate file (GATE_PATH is a relative constant)
    # so the new live-pass gate check doesn't read the real journal gate.
    monkeypatch.setattr(cron_gate, "GATE_PATH",
                        str(positions_json.parent / "cron_gate.json"))
    return ledger_dir, positions_json


@pytest.fixture(autouse=True)
def _no_edgar(monkeypatch):
    """Block live EDGAR calls in exits tests — return pe_expanded=False."""
    def _no_pe(**_):
        return SimpleNamespace(output={"pe_expanded": False})
    monkeypatch.setattr(exits, "pe_expansion_from_ticker", _no_pe)


def _seed_starter(
    paper_dirs,
    *,
    ticker,
    shares=10,
    fill_price=850.00,
    stop_price=820.00,
    stop_order_id=None,
    fill_date="2025-04-01",
):
    """Write a starter-state paper-auto ledger + matching positions.json entry.

    ``fill_date`` defaults far in the past so the post-entry grace period
    (2026-08-17) never engages in tests that aren't about it; grace tests
    pass a date at/near the synthetic df's last bar.
    """
    state.write_submitted_ledger(
        ticker=ticker, setup_type="EP", setup_grade="Swan",
        pivot_price=fill_price, limit_price=fill_price + 0.50,
        stop_price=stop_price, shares=shares,
        broker_order_id=10_000, broker="tiger_paper", sector_etf="XLK",
    )
    # Now mutate to starter (mimicking what reconcile would do).
    p = state.ledger_path(ticker)
    doc = yaml.safe_load(open(p))
    doc["meta"]["state"] = "starter"
    doc["position_state"]["starter"]["fill_price"] = float(fill_price)
    doc["position_state"]["starter"]["fill_date"] = fill_date
    if stop_order_id is not None:
        doc["position_state"]["stop_order_id"] = int(stop_order_id)
    state._validate_against_schema(doc)
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)

    state.append_to_positions_json({
        "ticker": ticker.upper(),
        "ledger_path": p.replace("\\", "/"),
        "entry_date": "2025-04-01",
        "entry_price": fill_price,
        "shares": shares,
        "stop": stop_price,
        "target_1": fill_price * 1.10,
        "sector": "XLK",
        "broker_order_id": 10_000,
        "broker": "tiger_paper",
        "stage": "starter",
        "setup_type": "EP",
        "setup_grade": "Swan",
    })


# ------------------------------------------------------- no-op


def test_no_starter_positions_returns_empty(paper_dirs):
    """When no paper-auto positions exist in starter state, evaluate_exits
    returns [] immediately — and does NOT construct a TigerClient (so this
    test runs without broker config)."""
    results = evaluate_exits()  # no client passed
    assert results == []


def test_only_submitted_positions_returns_empty(paper_dirs):
    """Positions in 'submitted' state are reconcile's job, not exits'."""
    state.write_submitted_ledger(
        ticker="NVDA", setup_type="EP", setup_grade="Swan",
        pivot_price=850.00, limit_price=850.50, stop_price=820.00,
        shares=10, broker_order_id=10001, broker="tiger_paper",
    )
    state.append_to_positions_json({
        "ticker": "NVDA", "ledger_path": "x.yml", "stage": "submitted",
        "shares": 10, "broker_order_id": 10001,
    })
    results = evaluate_exits()
    assert results == []


# ------------------------------------------------------- hold path


def test_hold_action_no_broker_calls(paper_dirs, monkeypatch):
    """When the composer returns 'hold', no place_order / cancel_order
    is issued, but a sell_eval_history entry is appended."""
    _seed_starter(paper_dirs, ticker="NVDA", shares=10, fill_price=850.00, stop_price=820.00)
    client = _client()

    # Force the composer to return hold.
    monkeypatch.setattr(
        exits, "sell_decision_compute",
        lambda **kw: SimpleNamespace(output={
            "action": "hold",
            "confidence": "HIGH",
            "contributing_triggers": [],
            "in_doubt_default_applied": False,
            "v1_preliminary_flag": True,
        }),
    )

    results = evaluate_exits(
        client=client,
        fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv()),
    )
    assert len(results) == 1
    r = results[0]
    assert r.action == "hold"
    assert r.placed is False
    assert r.sell_order_id is None

    # No broker order calls.
    assert [c for c in client._tc.calls if c[0] == "place_order"] == []
    assert [c for c in client._tc.calls if c[0] == "cancel_order"] == []

    # sell_eval_history appended on the ledger.
    doc = yaml.safe_load(open(state.ledger_path("NVDA")))
    assert doc["meta"]["state"] == "starter"     # unchanged
    history = doc.get("sell_eval_history", [])
    assert len(history) == 1
    assert history[0]["action"] == "hold"


# ------------------------------------------------------- sell path


def test_sell_50_places_limit_sell_and_marks_pending_close(paper_dirs, monkeypatch):
    """When the composer returns sell_50, evaluate_exits places a limit-sell
    at bid - 0.1% (approximated as last close), transitions the ledger to
    PENDING_CLOSE, and leaves the protective stop in place. The reconciler
    completes the lifecycle (-> closed on fill, -> starter on expiry).

    Behavior change 2026-06-02 (Bug 1 fix): exits.py used to mark the
    ledger closed + cancel the stop immediately after place_limit_sell
    returned an order_id. That stranded positions at Tiger with no
    monitor and no stop when DAY-TIF limit-sells expired unfilled.
    """
    _seed_starter(
        paper_dirs, ticker="NVDA", shares=10, fill_price=850.00,
        stop_price=820.00, stop_order_id=55_555,
    )
    client = _client()

    monkeypatch.setattr(
        exits, "sell_decision_compute",
        lambda **kw: SimpleNamespace(output={
            "action": "sell_50",
            "confidence": "MEDIUM",
            "contributing_triggers": ["climax_top_2 (count=2)"],
            "in_doubt_default_applied": False,
            "v1_preliminary_flag": True,
        }),
    )

    df = _synthetic_ohlcv(parabolic_tail=True)
    last_close = float(df["Close"].iloc[-1])
    expected_limit = round(last_close * (1.0 - exits.SELL_LIMIT_OFFSET_PCT), 2)

    results = evaluate_exits(
        client=client,
        fetch_ohlcv_fn=_fake_fetch(df),
    )
    r = results[0]
    assert r.action == "sell_50"
    assert r.placed is True
    assert r.sell_shares == 10
    assert r.sell_limit_price == expected_limit
    # New: exits.py NO LONGER cancels the stop. Reconciler does, on
    # confirmed fill.
    assert r.cancelled_stop_order_id is None

    # Broker got exactly ONE place_order call; no cancel.
    place_calls = [c for c in client._tc.calls if c[0] == "place_order"]
    cancel_calls = [c for c in client._tc.calls if c[0] == "cancel_order"]
    assert len(place_calls) == 1
    assert place_calls[0][1] == "SELL"
    assert place_calls[0][2] == 10
    assert place_calls[0][3] == "NVDA"
    assert place_calls[0][4] == expected_limit
    assert cancel_calls == []

    # Ledger transitioned to pending_close. Stop is STILL there, plus
    # the new pending_sell_order_id pointer.
    doc = yaml.safe_load(open(state.ledger_path("NVDA")))
    assert doc["meta"]["state"] == "pending_close"
    assert doc["meta"]["updated_by"] == "auto_paper/exits"
    assert doc["position_state"]["stop_order_id"] == 55_555
    assert "pending_sell_order_id" in doc["position_state"]
    history = doc["sell_eval_history"]
    assert history[-1]["action"] == "sell_50"

    # positions.json: entry STAYS, stage flips to pending_close. The
    # reconciler removes the entry on confirmed fill.
    data = json.load(open(state.PAPER_AUTO_POSITIONS_JSON))
    nvda_rows = [p for p in data["positions"] if p.get("ticker") == "NVDA"]
    assert len(nvda_rows) == 1
    assert nvda_rows[0]["stage"] == "pending_close"
    assert "pending_sell_order_id" in nvda_rows[0]


def test_sell_100_when_no_stop_present_still_marks_pending_close(paper_dirs, monkeypatch):
    """If the ledger never had a stop_order_id (e.g. earlier session failed
    to place one), the sell still proceeds. With the 2026-06-02 fix,
    the ledger transitions to pending_close (not closed) — reconciler
    completes the lifecycle on confirmed fill."""
    _seed_starter(
        paper_dirs, ticker="MSFT", shares=5, fill_price=400.00,
        stop_price=380.00, stop_order_id=None,
    )
    client = _client()

    monkeypatch.setattr(
        exits, "sell_decision_compute",
        lambda **kw: SimpleNamespace(output={
            "action": "sell_100",
            "confidence": "HIGH",
            "contributing_triggers": ["violations_3plus (count=3)"],
            "in_doubt_default_applied": False,
            "v1_preliminary_flag": True,
        }),
    )

    results = evaluate_exits(
        client=client,
        fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv()),
    )
    r = results[0]
    assert r.action == "sell_100"
    assert r.placed is True
    assert r.cancelled_stop_order_id is None   # nothing to cancel + exits.py never cancels

    place_calls = [c for c in client._tc.calls if c[0] == "place_order"]
    cancel_calls = [c for c in client._tc.calls if c[0] == "cancel_order"]
    assert len(place_calls) == 1
    assert place_calls[0][1] == "SELL"
    assert cancel_calls == []

    doc = yaml.safe_load(open(state.ledger_path("MSFT")))
    assert doc["meta"]["state"] == "pending_close"


# ------------------------------------------------------- dry-run


def test_dry_run_sell_does_not_call_broker_or_mutate(paper_dirs, monkeypatch):
    """Dry-run with a sell action: no place / cancel, ledger stays starter,
    no positions.json mutation."""
    _seed_starter(
        paper_dirs, ticker="NVDA", shares=10, fill_price=850.00,
        stop_price=820.00, stop_order_id=55_555,
    )
    client = _client()

    monkeypatch.setattr(
        exits, "sell_decision_compute",
        lambda **kw: SimpleNamespace(output={
            "action": "sell_75",
            "confidence": "HIGH",
            "contributing_triggers": ["climax_top_3plus (count=3)"],
            "in_doubt_default_applied": False,
            "v1_preliminary_flag": True,
        }),
    )

    results = evaluate_exits(
        client=client,
        dry_run=True,
        fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv(parabolic_tail=True)),
    )
    r = results[0]
    assert r.action == "sell_75"
    assert r.placed is False
    assert r.sell_limit_price is not None      # computed for visibility
    assert "dry_run" in (r.reason or "")

    # Zero broker calls.
    assert client._tc.calls == []

    # Ledger unchanged.
    doc = yaml.safe_load(open(state.ledger_path("NVDA")))
    assert doc["meta"]["state"] == "starter"
    assert doc["position_state"]["stop_order_id"] == 55_555

    # positions.json unchanged.
    data = json.load(open(state.PAPER_AUTO_POSITIONS_JSON))
    assert data["positions"][0]["stage"] == "starter"


# ------------------------------------------------------- error paths


def test_fetch_ohlcv_failure_marks_error(paper_dirs):
    _seed_starter(paper_dirs, ticker="NVDA", shares=10, fill_price=850.00, stop_price=820.00)
    client = _client()

    def _boom(*a, **kw):
        raise RuntimeError("YFINANCE_TIMEOUT")

    results = evaluate_exits(client=client, fetch_ohlcv_fn=_boom)
    assert len(results) == 1
    assert results[0].action == "error"
    assert "YFINANCE_TIMEOUT" in results[0].reason
    # No broker calls happened.
    assert client._tc.calls == []
    # Ledger untouched.
    doc = yaml.safe_load(open(state.ledger_path("NVDA")))
    assert doc["meta"]["state"] == "starter"


def test_place_sell_failure_surfaces_error(paper_dirs, monkeypatch):
    _seed_starter(paper_dirs, ticker="NVDA", shares=10, fill_price=850.00, stop_price=820.00)
    client = _client(place_raises=True)

    monkeypatch.setattr(
        exits, "sell_decision_compute",
        lambda **kw: SimpleNamespace(output={
            "action": "sell_100",
            "confidence": "HIGH",
            "contributing_triggers": ["violations_3plus"],
            "in_doubt_default_applied": False,
            "v1_preliminary_flag": True,
        }),
    )

    results = evaluate_exits(
        client=client,
        fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv()),
    )
    r = results[0]
    assert r.action == "error"
    assert "PLACE_FAILED" in r.reason

    # Ledger untouched (no state transition without a successful place).
    doc = yaml.safe_load(open(state.ledger_path("NVDA")))
    assert doc["meta"]["state"] == "starter"


def test_stop_not_cancelled_by_exits(paper_dirs, monkeypatch):
    """exits.py NO LONGER cancels the protective stop. With the 2026-06-02
    fix, the stop stays in place until the reconciler confirms the
    limit-sell filled. Any cancel-rejection scenario is therefore handled
    in tools.auto_paper.reconcile, not here. This test pins the new
    contract: zero cancel calls regardless of the broker's cancel-accept
    behavior."""
    _seed_starter(
        paper_dirs, ticker="NVDA", shares=10, fill_price=850.00,
        stop_price=820.00, stop_order_id=55_555,
    )
    # cancel_accepted=False would have surfaced as a non-fatal warning in
    # the OLD design. In the new design exits.py never asks, so the flag
    # is irrelevant. Setting it anyway documents that the test is
    # intentionally exercising the no-cancel path.
    client = _client(cancel_accepted=False)

    monkeypatch.setattr(
        exits, "sell_decision_compute",
        lambda **kw: SimpleNamespace(output={
            "action": "sell_100",
            "confidence": "HIGH",
            "contributing_triggers": ["violation_5"],
            "in_doubt_default_applied": False,
            "v1_preliminary_flag": True,
        }),
    )

    results = evaluate_exits(
        client=client,
        fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv()),
    )
    r = results[0]
    assert r.action == "sell_100"
    assert r.placed is True
    assert r.cancelled_stop_order_id is None

    # No cancel call ever attempted.
    cancel_calls = [c for c in client._tc.calls if c[0] == "cancel_order"]
    assert cancel_calls == []

    # Ledger transitioned to pending_close. Stop is INTACT.
    doc = yaml.safe_load(open(state.ledger_path("NVDA")))
    assert doc["meta"]["state"] == "pending_close"
    assert doc["position_state"]["stop_order_id"] == 55_555
    assert "pending_sell_order_id" in doc["position_state"]


# ------------------------------------------------------- 2026-06-02 bug fixes


def test_operator_locked_skips_all_transactions(paper_dirs, monkeypatch):
    """Bug 3 fix: position_state.operator_locked=true makes exits.py a
    no-op transactionally. The composer doesn't even run (early return
    in _evaluate_one). No broker calls, no ledger writes, no sell_eval
    appended. Result.action == 'operator_locked'."""
    _seed_starter(
        paper_dirs, ticker="NVDA", shares=10, fill_price=850.00,
        stop_price=820.00, stop_order_id=55_555,
    )
    # Set the operator_locked flag directly on the ledger.
    p = state.ledger_path("NVDA")
    doc = yaml.safe_load(open(p))
    doc["position_state"]["operator_locked"] = True
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)

    client = _client()

    # Composer would have returned sell_100 if reached — we monkeypatch
    # to make the test fail loudly if exits.py runs it anyway.
    composer_calls = {"n": 0}

    def _composer(**kw):
        composer_calls["n"] += 1
        return SimpleNamespace(output={
            "action": "sell_100", "confidence": "HIGH",
            "contributing_triggers": [], "in_doubt_default_applied": False,
            "v1_preliminary_flag": True,
        })
    monkeypatch.setattr(exits, "sell_decision_compute", _composer)

    results = evaluate_exits(
        client=client,
        fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv()),
    )
    r = results[0]
    assert r.action == "operator_locked"
    assert r.placed is False
    assert "operator_locked" in (r.reason or "")

    # Composer never ran (early return before _evaluate_one calls it).
    assert composer_calls["n"] == 0

    # Zero broker calls.
    assert client._tc.calls == []

    # Ledger untouched (no sell_eval append, no state change, stop still there).
    doc_after = yaml.safe_load(open(p))
    assert doc_after["meta"]["state"] == "starter"
    assert doc_after["position_state"]["stop_order_id"] == 55_555
    assert doc_after["position_state"]["operator_locked"] is True
    assert "sell_eval_history" not in doc_after or doc_after["sell_eval_history"] == []


def test_duplicate_sell_skipped_when_open_sell_already_pending(paper_dirs, monkeypatch):
    """Bug 2 fix: if there's already an open SELL at the broker for this
    ticker, exits.py does NOT stack another sell. sell_eval is still
    recorded; result.action == 'sell_pending_duplicate'.

    This is the 2026-05-28 COIN incident: four separate 213-sh SELL
    LMT fills stacked against a single 213-sh long → net short -639.
    """
    _seed_starter(
        paper_dirs, ticker="NVDA", shares=10, fill_price=850.00,
        stop_price=820.00, stop_order_id=55_555,
    )
    client = _client()

    monkeypatch.setattr(
        exits, "sell_decision_compute",
        lambda **kw: SimpleNamespace(output={
            "action": "sell_50", "confidence": "MEDIUM",
            "contributing_triggers": ["climax_top_2"],
            "in_doubt_default_applied": False, "v1_preliminary_flag": True,
        }),
    )

    # Inject the open-sell-tickers set directly — equivalent to Tiger
    # reporting an existing OPEN SELL on NVDA from a prior monitor tick.
    monkeypatch.setattr(
        exits, "_open_sell_tickers",
        lambda _client: {"NVDA"},
    )

    results = evaluate_exits(
        client=client,
        fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv(parabolic_tail=True)),
    )
    r = results[0]
    assert r.action == "sell_pending_duplicate"
    assert r.placed is False
    assert r.sell_order_id is None
    assert "pending" in (r.reason or "")

    # Zero place_order calls — the whole point of the idempotency fix.
    place_calls = [c for c in client._tc.calls if c[0] == "place_order"]
    assert place_calls == []

    # sell_eval still appended for audit trail.
    doc = yaml.safe_load(open(state.ledger_path("NVDA")))
    assert doc["meta"]["state"] == "starter"  # NO transition (sell wasn't placed)
    assert doc["position_state"]["stop_order_id"] == 55_555  # stop intact
    history = doc["sell_eval_history"]
    assert len(history) == 1
    assert history[0]["action"] == "sell_50"


# ---- STP-filter + stop-breach watchdog (2026-07-27 post-mortem review) ----


class _FakeOpenOrdersClient:
    """Duck-typed client exposing only open_orders(), for helper unit tests."""

    def __init__(self, orders):
        self._orders = orders

    def open_orders(self):
        return SimpleNamespace(output={"orders": self._orders, "n_orders": len(self._orders)})


def test_open_sell_helpers_filter_stp():
    """_open_sell_tickers must EXCLUDE resting protective stops (else every
    stop-protected position's composer exit is suppressed forever — the
    regression the 2026-07-27 review found); _open_stop_order_ids collects
    exactly those stops for the watchdog."""
    fake = _FakeOpenOrdersClient([
        {"order_id": 111, "symbol": "NVDA", "action": "SELL", "order_type": "STP"},
        {"order_id": 222, "symbol": "COIN", "action": "SELL", "order_type": "LMT"},
        {"order_id": 333, "symbol": "AAPL", "action": "BUY", "order_type": "LMT"},
        {"order_id": 444, "symbol": "MSFT", "action": "SELL", "order_type": "STP_LMT"},
    ])
    assert exits._open_sell_tickers(fake) == {"COIN"}
    assert exits._open_stop_order_ids(fake) == {111, 444}


def test_stop_breach_watchdog_forces_exit_and_cancels_zombie_stop(paper_dirs, monkeypatch):
    """COIN class (2026-06-01): last close BELOW a broker-confirmed resting
    stop = the stop failed to fire. Watchdog escalates to sell_100 through
    the working limit-sell path and cancels the zombie stop."""
    _seed_starter(
        paper_dirs, ticker="COIN", shares=10, fill_price=850.00,
        stop_price=820.00, stop_order_id=55_555,   # stop far above the ~$135 close
    )
    client = _client()
    monkeypatch.setattr(exits, "_open_sell_tickers", lambda _c: set())
    monkeypatch.setattr(exits, "_open_stop_order_ids", lambda _c: {55_555})
    monkeypatch.setattr(
        exits, "sell_decision_compute",
        lambda **kw: SimpleNamespace(output={
            "action": "hold", "confidence": "MEDIUM", "contributing_triggers": [],
            "in_doubt_default_applied": False, "v1_preliminary_flag": True,
        }),
    )

    results = evaluate_exits(
        client=client,
        fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv()),   # composer pinned to hold
        holdings={"COIN": 10.0},
    )
    r = results[0]
    assert r.action == "sell_100"
    assert r.placed is True
    assert r.cancelled_stop_order_id == 55_555
    assert r.contributing_triggers and "stop_breach_watchdog" in r.contributing_triggers[0]
    assert "stop_breach_watchdog" in (r.reason or "")

    place_calls = [c for c in client._tc.calls if c[0] == "place_order"]
    assert len(place_calls) == 1 and place_calls[0][1] == "SELL" and place_calls[0][2] == 10
    cancel_calls = [c for c in client._tc.calls if c[0] == "cancel_order"]
    assert len(cancel_calls) == 1 and cancel_calls[0][2] == 55_555

    doc = yaml.safe_load(open(state.ledger_path("COIN")))
    assert doc["meta"]["state"] == "pending_close"


def test_watchdog_stands_down_when_stop_not_resting(paper_dirs, monkeypatch):
    """Close below the recorded stop but NO matching open STP at the broker →
    the stop was likely consumed (filled) and reconcile owns the lifecycle;
    the watchdog must NOT fire a second sell."""
    _seed_starter(
        paper_dirs, ticker="COIN", shares=10, fill_price=850.00,
        stop_price=820.00, stop_order_id=55_555,
    )
    client = _client()
    monkeypatch.setattr(exits, "_open_sell_tickers", lambda _c: set())
    monkeypatch.setattr(exits, "_open_stop_order_ids", lambda _c: set())
    monkeypatch.setattr(
        exits, "sell_decision_compute",
        lambda **kw: SimpleNamespace(output={
            "action": "hold", "confidence": "MEDIUM", "contributing_triggers": [],
            "in_doubt_default_applied": False, "v1_preliminary_flag": True,
        }),
    )

    results = evaluate_exits(
        client=client,
        fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv()),
        holdings={"COIN": 10.0},
    )
    r = results[0]
    assert r.action == "hold"
    assert r.placed is False
    assert [c for c in client._tc.calls if c[0] == "place_order"] == []
    assert [c for c in client._tc.calls if c[0] == "cancel_order"] == []


# ---- Point-of-sale long-only guard (2026-07-24, second layer under the fix) ----
# exits.py sized SELLs from ledger `shares`, guarded only by shares <= 0, so a
# corrupt positive count (NFLX starter 29,760 while broker flat/short) would sell
# 29,760 from a flat/short book. Guard: refuse a SELL the broker can't back long.

_SELL_100 = {"action": "sell_100", "confidence": "HIGH",
             "contributing_triggers": ["violations_3plus"],
             "in_doubt_default_applied": False, "v1_preliminary_flag": True}
_SELL_50 = {"action": "sell_50", "confidence": "MEDIUM",
            "contributing_triggers": ["climax_top_2 (count=2)"],
            "in_doubt_default_applied": False, "v1_preliminary_flag": True}


def test_sell_refused_when_broker_short(paper_dirs, monkeypatch):
    """Composer wants sell_100 but broker is SHORT the name → refused, NO order."""
    _seed_starter(paper_dirs, ticker="NFLX", shares=29760, fill_price=81.59,
                  stop_price=77.23, stop_order_id=55_556)
    client = _client()
    monkeypatch.setattr(exits, "sell_decision_compute",
                        lambda **kw: SimpleNamespace(output=_SELL_100))
    results = evaluate_exits(
        client=client, fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv(parabolic_tail=True)),
        holdings={"NFLX": -29760},          # broker SHORT (the incident)
    )
    r = results[0]
    assert r.action == "refused_short_guard"
    assert r.placed is False
    assert [c for c in client._tc.calls if c[0] == "place_order"] == []
    # ledger NOT flipped to pending_close
    assert yaml.safe_load(open(state.ledger_path("NFLX")))["meta"]["state"] == "starter"


def test_sell_refused_when_broker_flat(paper_dirs, monkeypatch):
    """Broker flat (ticker absent from holdings) + positive ledger shares →
    refused — selling would open a naked short."""
    _seed_starter(paper_dirs, ticker="NVDA", shares=10, fill_price=850.00,
                  stop_price=820.00, stop_order_id=55_557)
    client = _client()
    monkeypatch.setattr(exits, "sell_decision_compute",
                        lambda **kw: SimpleNamespace(output=_SELL_50))
    results = evaluate_exits(
        client=client, fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv(parabolic_tail=True)),
        holdings={"OTHER": 100},            # NVDA absent from broker = flat
    )
    assert results[0].action == "refused_short_guard"
    assert [c for c in client._tc.calls if c[0] == "place_order"] == []


def test_sell_proceeds_when_broker_long_backs_it(paper_dirs, monkeypatch):
    """Guard does NOT over-block: broker holds the full long → sell places."""
    _seed_starter(paper_dirs, ticker="NVDA", shares=10, fill_price=850.00,
                  stop_price=820.00, stop_order_id=55_558)
    client = _client()
    monkeypatch.setattr(exits, "sell_decision_compute",
                        lambda **kw: SimpleNamespace(output=_SELL_50))
    results = evaluate_exits(
        client=client, fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv(parabolic_tail=True)),
        holdings={"NVDA": 10},              # broker fully backs the long
    )
    r = results[0]
    assert r.action == "sell_50" and r.placed is True
    assert len([c for c in client._tc.calls if c[0] == "place_order"]) == 1


# ---- cron-gate honoring (2026-07-24, Alfred Q2): monitor stands down when gated ----

def test_exits_stands_down_when_cron_gated(paper_dirs):
    """A LIVE monitor pass with the cron gate set -> cron_gated, NO placement."""
    _seed_starter(paper_dirs, ticker="NVDA", shares=10, fill_price=850.00,
                  stop_price=820.00, stop_order_id=55_559)
    cron_gate.set_gate("short_anomaly", {"short_anomaly": ["NFLX"]})
    client = _client()
    results = evaluate_exits(
        client=client, fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv(parabolic_tail=True)))
    assert [r.action for r in results] == ["cron_gated"]
    assert [c for c in client._tc.calls if c[0] == "place_order"] == []


def test_exits_dry_run_still_runs_when_gated(paper_dirs, monkeypatch):
    """dry_run bypasses the stand-down so the operator can inspect while gated."""
    _seed_starter(paper_dirs, ticker="NVDA", shares=10, fill_price=850.00,
                  stop_price=820.00, stop_order_id=55_560)
    cron_gate.set_gate("short_anomaly", {"short_anomaly": ["NFLX"]})
    monkeypatch.setattr(exits, "sell_decision_compute",
                        lambda **kw: SimpleNamespace(output=_SELL_50))
    results = evaluate_exits(
        dry_run=True, fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv(parabolic_tail=True)))
    assert results[0].action != "cron_gated"   # ran the composer, did not stand down


# ---- cancel-stop-then-sell (2026-08-13 — closes the 2026-07-27 OPEN bug) ----
# Tiger validates SELL qty against UNENCUMBERED holdings at submission, so a
# resting full-size STP got every composer exit rejected on arrival (EXPIRED /
# filled=0, GO order #44074483903777792). The exit path now cancels a
# broker-confirmed resting stop BEFORE placing, re-arms it on any placement
# failure, and refuses to transition to pending_close on a vanished sell.


class _SellFailsStopOkTradeClient(FakeTradeClient):
    """place_order fails for LIMIT orders (the exit) but accepts STP orders
    (aux_price set) — reproduces 'exit lost, stop re-arm succeeds'."""

    def place_order(self, order):
        if getattr(order, "aux_price", None) is None:
            raise RuntimeError("SELL_REJECTED")
        return super().place_order(order)


class _OrdersQueryableTradeClient(FakeTradeClient):
    """FakeTradeClient + open/filled order queries returning empty lists —
    _sell_gone_at_broker then sees the just-placed sell as vanished, i.e.
    the instantly-EXPIRED at-submission rejection shape."""

    def get_open_orders(self, *, account, **_):
        return []

    def get_filled_orders(self, *, account, **_):
        return []


def _pin_sell_50(monkeypatch):
    monkeypatch.setattr(
        exits, "sell_decision_compute",
        lambda **kw: SimpleNamespace(output={
            "action": "sell_50",
            "confidence": "MEDIUM",
            "contributing_triggers": ["climax_top_2 (count=2)"],
            "in_doubt_default_applied": False,
            "v1_preliminary_flag": True,
        }),
    )


def test_sell_cancels_broker_confirmed_stop_before_placement(paper_dirs, monkeypatch):
    """Composer exit on a stop-protected position: the broker-confirmed
    resting STP is cancelled BEFORE the limit-sell is placed, the ledger's
    stop_order_id is cleared (the id is dead), and the position still
    transitions to pending_close. Stop seeded BELOW the synthetic close so
    the watchdog stands down and the composer's sell_50 drives."""
    _seed_starter(paper_dirs, ticker="NVDA", shares=10, fill_price=100.00,
                  stop_price=80.00, stop_order_id=55_555)
    client = _client()
    monkeypatch.setattr(exits, "_open_sell_tickers", lambda _c: set())
    monkeypatch.setattr(exits, "_open_stop_order_ids", lambda _c: {55_555})
    _pin_sell_50(monkeypatch)

    results = evaluate_exits(
        client=client,
        fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv(parabolic_tail=True)),
        holdings={"NVDA": 10.0},
    )
    r = results[0]
    assert r.action == "sell_50"
    assert r.placed is True
    assert r.cancelled_stop_order_id == 55_555
    assert "cancelled pre-placement" in (r.reason or "")

    calls = client._tc.calls
    cancel_idx = next(i for i, cl in enumerate(calls)
                      if cl[0] == "cancel_order" and cl[2] == 55_555)
    place_idx = next(i for i, cl in enumerate(calls) if cl[0] == "place_order")
    assert cancel_idx < place_idx, "stop must be cancelled BEFORE the exit is placed"

    doc = yaml.safe_load(open(state.ledger_path("NVDA")))
    assert doc["meta"]["state"] == "pending_close"
    assert "stop_order_id" not in doc["position_state"]
    assert "cancel-stop-then-sell" in doc["notes"]


def test_cancel_rejected_blocks_exit(paper_dirs, monkeypatch):
    """Broker refuses the stop cancel -> the exit is NOT placed; the position
    keeps its stop protection and stays in starter for the next tick."""
    _seed_starter(paper_dirs, ticker="NVDA", shares=10, fill_price=850.00,
                  stop_price=820.00, stop_order_id=55_555)
    client = _client(cancel_accepted=False)
    monkeypatch.setattr(exits, "_open_sell_tickers", lambda _c: set())
    monkeypatch.setattr(exits, "_open_stop_order_ids", lambda _c: {55_555})
    _pin_sell_50(monkeypatch)

    results = evaluate_exits(
        client=client,
        fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv(parabolic_tail=True)),
        holdings={"NVDA": 10.0},
    )
    r = results[0]
    assert r.action == "error"
    assert "exit NOT placed" in (r.reason or "")
    assert [c for c in client._tc.calls if c[0] == "place_order"] == []

    doc = yaml.safe_load(open(state.ledger_path("NVDA")))
    assert doc["meta"]["state"] == "starter"
    assert doc["position_state"]["stop_order_id"] == 55_555


def test_exit_place_failure_rearms_stop(paper_dirs, monkeypatch):
    """Stop cancelled, then the exit placement raises -> the protective stop
    is re-armed at the ledger stop price (ratchet recovery contract) and the
    ledger records the new stop id; no pending_close transition."""
    from tools.broker.tiger import TigerClient
    _seed_starter(paper_dirs, ticker="NVDA", shares=10, fill_price=850.00,
                  stop_price=820.00, stop_order_id=55_555)
    client = TigerClient(_trade_client=_SellFailsStopOkTradeClient())
    monkeypatch.setattr(exits, "_open_sell_tickers", lambda _c: set())
    monkeypatch.setattr(exits, "_open_stop_order_ids", lambda _c: {55_555})
    _pin_sell_50(monkeypatch)

    results = evaluate_exits(
        client=client,
        fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv(parabolic_tail=True)),
        holdings={"NVDA": 10.0},
    )
    r = results[0]
    assert r.action == "error"
    assert "place_limit_sell" in (r.reason or "")
    assert "re-armed" in (r.reason or "")

    # Exactly one successful order at the broker: the re-armed STP @ 820.
    place_calls = [c for c in client._tc.calls if c[0] == "place_order"]
    assert len(place_calls) == 1
    assert place_calls[0][1] == "SELL" and place_calls[0][5] == 820.00

    doc = yaml.safe_load(open(state.ledger_path("NVDA")))
    assert doc["meta"]["state"] == "starter"          # never pending_close
    assert doc["position_state"]["stop_order_id"] == 90_000
    assert "TEMPORARILY UNPROTECTED" not in doc["notes"]
    assert "re-armed stop" in doc["notes"]


def test_submission_rejection_detected_no_pending_close(paper_dirs, monkeypatch):
    """The 2026-07-27 failure shape: place_limit_sell returns an order id but
    the order is instantly gone at the broker (EXPIRED at submission). The
    guard must refuse the pending_close transition and re-arm the stop —
    no more phantom pending_close."""
    from tools.broker.tiger import TigerClient
    _seed_starter(paper_dirs, ticker="GO", shares=10, fill_price=8.04,
                  stop_price=8.43, stop_order_id=55_555)
    client = TigerClient(_trade_client=_OrdersQueryableTradeClient())
    monkeypatch.setattr(exits, "_open_sell_tickers", lambda _c: set())
    monkeypatch.setattr(exits, "_open_stop_order_ids", lambda _c: {55_555})
    _pin_sell_50(monkeypatch)

    results = evaluate_exits(
        client=client,
        fetch_ohlcv_fn=_fake_fetch(_synthetic_ohlcv(parabolic_tail=True)),
        holdings={"GO": 10.0},
    )
    r = results[0]
    assert r.action == "error"
    assert "rejected at submission" in (r.reason or "")
    assert r.cancelled_stop_order_id == 55_555

    doc = yaml.safe_load(open(state.ledger_path("GO")))
    assert doc["meta"]["state"] == "starter"          # NOT pending_close
    assert "pending_sell_order_id" not in doc["position_state"]
    # Stop re-armed under a fresh broker id (the sell consumed 90_000).
    assert doc["position_state"]["stop_order_id"] == 90_001
    assert "cancel-stop-then-sell" in doc["notes"]


# ---- Post-entry grace period (2026-08-17 — the 2026-08-14 day-0 exit class) ----
# The simulator only consults the sell-composer once offset > grace_period_bars
# (fill bar = offset 0; tools/backtest/sell_aware.py grace_period_bars=3,
# simulator.py step 5). The live monitor had no equivalent: on 2026-08-14 the
# 13:32 ET tick evaluated three fills ~2h old against full-day OHLCV that
# predates their entries and same-day-exited MU/STX/WDC (violations_1 +
# sell_into_strength 0.8 — the pre-entry run-up ts_momentum SELECTS FOR).
# Grace suppresses the TRANSACTION only; the verdict is still recorded.


def _fill_date_at_bar(df, bars_held):
    """ISO fill_date such that the df's last bar sits ``bars_held`` bars
    after the fill bar (simulator offset semantics: fill bar = offset 0)."""
    return df.index[-(bars_held + 1)].date().isoformat()


def test_grace_period_suppresses_day0_composer_sell(paper_dirs, monkeypatch):
    """The MU replay (2026-08-14): position filled TODAY (bar 0), composer
    fires sell_50 on pre-entry OHLCV → verdict recorded, NOTHING transacted:
    no sell placed, stop NOT cancelled, ledger stays starter.

    start_close=1000 keeps the synthetic close ABOVE the 880 stop so the
    stop-breach watchdog (correctly grace-exempt) stands down here."""
    df = _synthetic_ohlcv(start_close=1000.0, parabolic_tail=True)
    _seed_starter(paper_dirs, ticker="MU", shares=43, fill_price=969.25,
                  stop_price=880.00, stop_order_id=55_555,
                  fill_date=_fill_date_at_bar(df, 0))
    client = _client()
    monkeypatch.setattr(exits, "_open_sell_tickers", lambda _c: set())
    monkeypatch.setattr(exits, "_open_stop_order_ids", lambda _c: {55_555})
    _pin_sell_50(monkeypatch)

    results = evaluate_exits(
        client=client, fetch_ohlcv_fn=_fake_fetch(df), holdings={"MU": 43.0},
    )
    r = results[0]
    assert r.action == "sell_50"            # the composer's true verdict, preserved
    assert r.placed is False
    assert r.grace_suppressed is True
    assert "grace period" in (r.reason or "")
    assert r.contributing_triggers and "grace_period" in r.contributing_triggers[0]

    # NO broker transactions of any kind.
    assert [c for c in client._tc.calls if c[0] == "place_order"] == []
    assert [c for c in client._tc.calls if c[0] == "cancel_order"] == []

    # Ledger: still starter, stop intact, verdict recorded with the flag.
    doc = yaml.safe_load(open(state.ledger_path("MU")))
    assert doc["meta"]["state"] == "starter"
    assert doc["position_state"]["stop_order_id"] == 55_555
    history = doc["sell_eval_history"]
    assert len(history) == 1
    assert history[0]["action"] == "sell_50"
    assert history[0]["grace_suppressed"] is True
    assert history[0]["bars_held"] == 0


def test_grace_period_boundary_bar3_suppressed(paper_dirs, monkeypatch):
    """offset 3 is the LAST suppressed bar (simulator: composer runs only when
    offset > grace_period_bars=3)."""
    df = _synthetic_ohlcv(parabolic_tail=True)
    _seed_starter(paper_dirs, ticker="NVDA", shares=10, fill_price=100.00,
                  stop_price=80.00, stop_order_id=55_555,
                  fill_date=_fill_date_at_bar(df, 3))
    client = _client()
    monkeypatch.setattr(exits, "_open_sell_tickers", lambda _c: set())
    monkeypatch.setattr(exits, "_open_stop_order_ids", lambda _c: {55_555})
    _pin_sell_50(monkeypatch)

    results = evaluate_exits(
        client=client, fetch_ohlcv_fn=_fake_fetch(df), holdings={"NVDA": 10.0},
    )
    assert results[0].grace_suppressed is True
    assert results[0].placed is False
    assert [c for c in client._tc.calls if c[0] == "place_order"] == []
    assert yaml.safe_load(open(state.ledger_path("NVDA")))["meta"]["state"] == "starter"


def test_grace_period_expired_bar4_transacts(paper_dirs, monkeypatch):
    """offset 4 = first bar PAST grace → the normal cancel-stop-then-sell exit
    path runs unchanged (mirrors test_sell_cancels_broker_confirmed_stop_...)."""
    df = _synthetic_ohlcv(parabolic_tail=True)
    _seed_starter(paper_dirs, ticker="NVDA", shares=10, fill_price=100.00,
                  stop_price=80.00, stop_order_id=55_555,
                  fill_date=_fill_date_at_bar(df, 4))
    client = _client()
    monkeypatch.setattr(exits, "_open_sell_tickers", lambda _c: set())
    monkeypatch.setattr(exits, "_open_stop_order_ids", lambda _c: {55_555})
    _pin_sell_50(monkeypatch)

    results = evaluate_exits(
        client=client, fetch_ohlcv_fn=_fake_fetch(df), holdings={"NVDA": 10.0},
    )
    r = results[0]
    assert r.action == "sell_50"
    assert r.placed is True
    assert r.grace_suppressed is False
    assert r.cancelled_stop_order_id == 55_555
    assert yaml.safe_load(open(state.ledger_path("NVDA")))["meta"]["state"] == "pending_close"


def test_watchdog_overrides_grace_period(paper_dirs, monkeypatch):
    """A broker-confirmed FAILED stop (close below a still-open STP) must
    force the exit even on bar 0 — grace only suppresses composer
    discretion, never stop protection."""
    df = _synthetic_ohlcv()   # close ≈ 135, far below the 820 stop
    _seed_starter(paper_dirs, ticker="COIN", shares=10, fill_price=850.00,
                  stop_price=820.00, stop_order_id=55_555,
                  fill_date=_fill_date_at_bar(df, 0))
    client = _client()
    monkeypatch.setattr(exits, "_open_sell_tickers", lambda _c: set())
    monkeypatch.setattr(exits, "_open_stop_order_ids", lambda _c: {55_555})
    _pin_sell_50(monkeypatch)   # composer wants sell_50 → grace would suppress

    results = evaluate_exits(
        client=client, fetch_ohlcv_fn=_fake_fetch(df), holdings={"COIN": 10.0},
    )
    r = results[0]
    assert r.action == "sell_100"           # watchdog escalation, not the composer
    assert r.placed is True
    assert r.grace_suppressed is False
    assert r.contributing_triggers and "stop_breach_watchdog" in r.contributing_triggers[0]
    assert yaml.safe_load(open(state.ledger_path("COIN")))["meta"]["state"] == "pending_close"


def test_grace_dry_run_records_verdict_no_broker(paper_dirs, monkeypatch):
    """dry_run inside grace: the suppressed verdict surfaces on the
    ExitResult (dry_run never persists ledgers) and no client is needed."""
    df = _synthetic_ohlcv(parabolic_tail=True)
    _seed_starter(paper_dirs, ticker="MU", shares=43, fill_price=969.25,
                  stop_price=880.00, fill_date=_fill_date_at_bar(df, 1))
    _pin_sell_50(monkeypatch)

    results = evaluate_exits(dry_run=True, fetch_ohlcv_fn=_fake_fetch(df))
    r = results[0]
    assert r.action == "sell_50"
    assert r.placed is False
    assert r.grace_suppressed is True
