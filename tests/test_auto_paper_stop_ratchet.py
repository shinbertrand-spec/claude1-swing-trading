"""Tests for tools.auto_paper.stop_ratchet — trailing-stop ratchet.

Covers:
  * no-op when no starter positions
  * skip when no broker stop (no stop_order_id)
  * no_change when gain below tier-1 (+5%)
  * no_change when current stop already at/above target
  * tier-1 ratchet: gain >= +5% → stop to break-even
  * tier-2 ratchet: gain >= +10% → stop to +5%
  * dry-run does not call broker, does not write ledger
  * cancel rejected → no place, action=error
  * place_stop_loss fails after cancel → ledger clears stop_order_id + records UNPROTECTED note
  * positions.json stop field updated on successful ratchet

Never hits live broker / OHLCV / EDGAR — everything injected.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from tools.auto_paper import state
from tools.auto_paper.stop_ratchet import ratchet_all


# --- fakes (mirrored from test_auto_paper_exits.py) -------------------


class FakeTradeClient:
    def __init__(self, *, cancel_accepted=True, place_raises=False):
        self.account_full = "PAPER87654321"
        self.config_info = {
            "tiger_id_masked": "...5678",
            "account_masked": "...4321",
            "license": "TBSG",
            "is_paper": True,
            "server_url": "https://mock",
            "props_dir": "/mock",
        }
        self.calls: list[tuple] = []
        self._next_order_id = 70_000
        self._cancel_accepted = cancel_accepted
        self._place_raises = place_raises

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


def _fake_fetch(last_close):
    """Build a fetch_ohlcv_fn that returns a tiny df ending at last_close."""
    def _f(ticker, period="1mo", interval="1d"):
        df = pd.DataFrame({"Close": [last_close - 1.0, last_close]})
        return SimpleNamespace(df=df, fetched_at="2026-05-25T00:00:00+00:00",
                               source=f"fake:{ticker}",
                               ticker=ticker, period=period, interval=interval)
    return _f


# --- fixtures ---------------------------------------------------------


@pytest.fixture
def paper_dirs(tmp_path, monkeypatch):
    ledger_dir = tmp_path / "ledgers" / "paper-auto"
    positions_json = tmp_path / "journal" / "paper-auto" / "positions.json"
    monkeypatch.setattr(state, "PAPER_AUTO_LEDGER_DIR", str(ledger_dir))
    monkeypatch.setattr(state, "PAPER_AUTO_POSITIONS_JSON", str(positions_json))
    return ledger_dir, positions_json


def _seed_starter(
    paper_dirs,
    *,
    ticker,
    shares=10,
    fill_price=100.00,
    stop_price=92.00,
    stop_order_id=70_001,
    current_stop=None,
):
    """Write a starter-state paper-auto ledger + matching positions.json entry."""
    state.write_submitted_ledger(
        ticker=ticker, setup_type="EP", setup_grade="Swan",
        pivot_price=fill_price, limit_price=fill_price + 0.50,
        stop_price=stop_price, shares=shares,
        broker_order_id=10_000, broker="tiger_paper", sector_etf="XLK",
    )
    p = state.ledger_path(ticker)
    doc = yaml.safe_load(open(p))
    doc["meta"]["state"] = "starter"
    doc["position_state"]["starter"]["fill_price"] = float(fill_price)
    doc["position_state"]["starter"]["fill_date"] = "2025-04-01"
    if stop_order_id is not None:
        doc["position_state"]["stop_order_id"] = int(stop_order_id)
    if current_stop is not None:
        doc["position_state"]["current_stop"] = float(current_stop)
    state._validate_against_schema(doc)
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)

    state.append_to_positions_json({
        "ticker": ticker.upper(),
        "ledger_path": p.replace("\\", "/"),
        "entry_date": "2025-04-01",
        "entry_price": fill_price,
        "shares": shares,
        "stop": current_stop if current_stop is not None else stop_price,
        "target_1": fill_price * 1.10,
        "sector": "XLK",
        "broker_order_id": 10_000,
        "broker": "tiger_paper",
        "stage": "starter",
        "setup_type": "EP",
        "setup_grade": "Swan",
    })
    return p


# --- no-op cases ------------------------------------------------------


def test_no_starter_positions_returns_empty(paper_dirs):
    assert ratchet_all() == []


def test_position_without_stop_order_id_returns_no_stop(paper_dirs):
    _seed_starter(paper_dirs, ticker="NOSTOP", fill_price=100.0,
                  stop_price=92.0, stop_order_id=None)
    client = _client()
    results = ratchet_all(client=client, fetch_ohlcv_fn=_fake_fetch(108.0))
    assert len(results) == 1
    assert results[0].action == "no_stop"
    assert results[0].fill_price == pytest.approx(100.0)


def test_gain_below_tier_1_returns_no_change(paper_dirs):
    """Gain +3% < tier-1 (5%) → no ratchet."""
    _seed_starter(paper_dirs, ticker="LOW", fill_price=100.0, stop_price=92.0)
    client = _client()
    results = ratchet_all(client=client, fetch_ohlcv_fn=_fake_fetch(103.0))
    assert len(results) == 1
    r = results[0]
    assert r.action == "no_change"
    assert r.gain_pct == pytest.approx(0.03)
    assert r.new_stop is None


def test_current_stop_already_at_target_returns_no_change(paper_dirs):
    """Gain +6% qualifies for tier-1 (BE = $100), but current_stop already $100."""
    _seed_starter(paper_dirs, ticker="ATBE", fill_price=100.0,
                  stop_price=92.0, current_stop=100.0)
    client = _client()
    results = ratchet_all(client=client, fetch_ohlcv_fn=_fake_fetch(106.0))
    r = results[0]
    assert r.action == "no_change"
    assert r.tier == 1
    assert r.new_stop == pytest.approx(100.0)


# --- tier-1 (+5% → break-even) ---------------------------------------


def test_tier_1_ratchets_stop_to_break_even(paper_dirs):
    """Gain +6%, original stop $92 → ratchet to $100 (entry)."""
    ledger_path = _seed_starter(paper_dirs, ticker="T1", fill_price=100.0,
                                stop_price=92.0)
    client = _client()
    results = ratchet_all(client=client, fetch_ohlcv_fn=_fake_fetch(106.0))
    r = results[0]
    assert r.action == "ratcheted"
    assert r.tier == 1
    assert r.new_stop == pytest.approx(100.0)
    assert r.old_stop == pytest.approx(92.0)
    assert r.new_stop_order_id is not None
    assert r.new_stop_order_id != r.old_stop_order_id

    # Ledger updated
    doc = yaml.safe_load(open(ledger_path))
    assert doc["position_state"]["current_stop"] == pytest.approx(100.0)
    assert doc["position_state"]["stop_order_id"] == r.new_stop_order_id
    assert "tier-1 ratchet" in doc["notes"]


# --- tier-2 (+10% → entry × 1.05) ------------------------------------


def test_tier_2_ratchets_stop_to_plus_5(paper_dirs):
    """Gain +12%, current_stop $100 (already BE'd) → ratchet to $105."""
    ledger_path = _seed_starter(paper_dirs, ticker="T2", fill_price=100.0,
                                stop_price=92.0, current_stop=100.0)
    client = _client()
    results = ratchet_all(client=client, fetch_ohlcv_fn=_fake_fetch(112.0))
    r = results[0]
    assert r.action == "ratcheted"
    assert r.tier == 2
    assert r.new_stop == pytest.approx(105.0)
    assert r.old_stop == pytest.approx(100.0)

    doc = yaml.safe_load(open(ledger_path))
    assert doc["position_state"]["current_stop"] == pytest.approx(105.0)


def test_tier_2_from_fresh_position_skips_be_jumps_to_plus_5(paper_dirs):
    """Gain +15% on a never-ratcheted position → goes straight to tier-2."""
    _seed_starter(paper_dirs, ticker="JUMP", fill_price=100.0, stop_price=92.0)
    client = _client()
    results = ratchet_all(client=client, fetch_ohlcv_fn=_fake_fetch(115.0))
    r = results[0]
    assert r.action == "ratcheted"
    assert r.tier == 2
    assert r.new_stop == pytest.approx(105.0)


# --- dry-run ----------------------------------------------------------


def test_dry_run_does_not_call_broker(paper_dirs):
    ledger_path = _seed_starter(paper_dirs, ticker="DRY", fill_price=100.0,
                                stop_price=92.0)
    client = _client()
    fake = client._tc  # noqa: SLF001
    results = ratchet_all(client=client, dry_run=True,
                          fetch_ohlcv_fn=_fake_fetch(106.0))
    r = results[0]
    assert r.action == "dry_run"
    assert r.new_stop == pytest.approx(100.0)
    assert fake.calls == []  # no broker calls

    # Ledger unchanged (still has original stop_order_id, no current_stop)
    doc = yaml.safe_load(open(ledger_path))
    assert "current_stop" not in (doc.get("position_state") or {}) or \
           doc["position_state"]["current_stop"] != pytest.approx(100.0)


# --- failure modes ----------------------------------------------------


def test_cancel_rejected_yields_error_no_place(paper_dirs):
    _seed_starter(paper_dirs, ticker="REJ", fill_price=100.0, stop_price=92.0)
    client = _client(cancel_accepted=False)
    results = ratchet_all(client=client, fetch_ohlcv_fn=_fake_fetch(106.0))
    r = results[0]
    assert r.action == "error"
    assert "rejected cancel" in r.reason


def test_place_failure_after_cancel_marks_unprotected(paper_dirs):
    """If place_stop_loss raises after cancel succeeded, the ledger must
    record the temporary-unprotected state + clear stop_order_id."""
    ledger_path = _seed_starter(paper_dirs, ticker="UNP", fill_price=100.0,
                                stop_price=92.0, stop_order_id=70_001)
    client = _client(place_raises=True)
    results = ratchet_all(client=client, fetch_ohlcv_fn=_fake_fetch(106.0))
    r = results[0]
    assert r.action == "error"
    assert "CANCELLED" in r.reason
    assert "place_stop_loss FAILED" in r.reason

    doc = yaml.safe_load(open(ledger_path))
    assert "stop_order_id" not in doc.get("position_state", {})
    assert "UNPROTECTED" in doc["notes"]


# --- positions.json side effect --------------------------------------


def test_positions_json_stop_field_updated_on_ratchet(paper_dirs):
    _ledger_dir, positions_json = paper_dirs
    _seed_starter(paper_dirs, ticker="PJ", fill_price=100.0, stop_price=92.0)
    client = _client()
    ratchet_all(client=client, fetch_ohlcv_fn=_fake_fetch(106.0))
    data = json.loads(open(positions_json).read())
    pj_entry = [p for p in data["positions"] if p["ticker"] == "PJ"][0]
    assert pj_entry["stop"] == pytest.approx(100.0)


def test_dry_run_does_not_update_positions_json(paper_dirs):
    _ledger_dir, positions_json = paper_dirs
    _seed_starter(paper_dirs, ticker="PJD", fill_price=100.0, stop_price=92.0)
    client = _client()
    ratchet_all(client=client, dry_run=True, fetch_ohlcv_fn=_fake_fetch(106.0))
    data = json.loads(open(positions_json).read())
    pj_entry = [p for p in data["positions"] if p["ticker"] == "PJD"][0]
    assert pj_entry["stop"] == pytest.approx(92.0)  # unchanged


# --- multi-position --------------------------------------------------


def test_multi_position_each_evaluated_independently(paper_dirs):
    _seed_starter(paper_dirs, ticker="HOLDING", fill_price=100.0,
                  stop_price=92.0)  # default gain reading will be controlled
    _seed_starter(paper_dirs, ticker="MOVING", fill_price=50.0,
                  stop_price=46.0)

    # Per-ticker price control
    def per_ticker_fetch(ticker, period="1mo", interval="1d"):
        prices = {"HOLDING": 102.0, "MOVING": 56.0}  # +2%, +12%
        last = prices[ticker]
        df = pd.DataFrame({"Close": [last - 0.5, last]})
        return SimpleNamespace(df=df, fetched_at="x", source=f"fake:{ticker}",
                               ticker=ticker, period=period, interval=interval)

    client = _client()
    results = ratchet_all(client=client, fetch_ohlcv_fn=per_ticker_fetch)
    by_ticker = {r.ticker: r for r in results}
    assert by_ticker["HOLDING"].action == "no_change"
    assert by_ticker["MOVING"].action == "ratcheted"
    assert by_ticker["MOVING"].tier == 2
