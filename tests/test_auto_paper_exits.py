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

from tools.auto_paper import exits, state
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
):
    """Write a starter-state paper-auto ledger + matching positions.json entry."""
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
    doc["position_state"]["starter"]["fill_date"] = "2025-04-01"
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
