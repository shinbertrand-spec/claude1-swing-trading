"""Tests for the event_insider_buying KIND + insider_events file layer."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from tools.fundamentals.insider_events import (
    InsiderEvent,
    build_events,
    load_events,
    write_events,
)
from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._kinds import event_insider_buying as K


# ---- registration -------------------------------------------------------


def test_kind_registered():
    assert K.KIND == "event_insider_buying"
    assert KIND_REGISTRY[K.KIND] is K


def test_kind_is_momentum_class_for_fills():
    from tools.auto_paper.entry_pricing import MOMENTUM_KINDS
    assert "event_insider_buying" in MOMENTUM_KINDS


# ---- events file write/load/filter --------------------------------------


def _ev(ticker, d, level="high", score=3.6):
    return InsiderEvent(ticker=ticker, event_date=d, conviction_level=level,
                        composite_score=score, n_insiders=2, best_tier="elite",
                        total_value=100000.0)


def test_write_then_load_roundtrip(tmp_path):
    path = tmp_path / "events.yml"
    write_events(path, [_ev("ABC", "2024-03-15"), _ev("XYZ", "2024-04-01")],
                 meta={"universe": "test"})
    loaded = load_events(path)
    assert set(loaded.keys()) == {"ABC", "XYZ"}
    assert loaded["ABC"][0].conviction_level == "high"


def test_load_filters_min_conviction(tmp_path):
    path = tmp_path / "events.yml"
    write_events(path, [
        _ev("ABC", "2024-03-15", level="high"),
        _ev("LOW", "2024-03-16", level="low"),
        _ev("MED", "2024-03-17", level="medium"),
    ], meta={})
    loaded = load_events(path, min_conviction="medium")
    assert set(loaded.keys()) == {"ABC", "MED"}   # low excluded


def test_load_filters_universe(tmp_path):
    path = tmp_path / "events.yml"
    write_events(path, [_ev("ABC", "2024-03-15"), _ev("XYZ", "2024-04-01")], meta={})
    loaded = load_events(path, universe={"ABC"})
    assert set(loaded.keys()) == {"ABC"}


def test_load_missing_file_returns_empty(tmp_path):
    assert load_events(tmp_path / "nope.yml") == {}


# ---- replay -------------------------------------------------------------


def _ohlcv(start="2024-01-01", n=200, price=100.0):
    idx = pd.bdate_range(start=start, periods=n)
    # gentle uptrend + nonzero range so ATR computes
    closes = [price + i * 0.5 for i in range(n)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    opens = [c for c in closes]
    return pd.DataFrame({"Open": opens, "High": highs, "Low": lows,
                         "Close": closes, "Volume": [1_000_000] * n}, index=idx)


def test_replay_emits_signal_next_bar_after_event():
    df = _ohlcv()
    # event on a date; entry must be the first bar strictly after it
    ev_date = df.index[50].date()
    state = K.EventState({"ABC": [_ev("ABC", ev_date.isoformat())]})
    sigs = K.replay(df, "ABC", {"max_hold_days": 126}, state)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.setup_type == "event_insider_buying"
    assert s.entry_date == ev_date
    assert s.fill_date == df.index[51].date()      # next bar
    assert s.entry_price == pytest.approx(float(df.iloc[51]["Open"]))
    assert s.max_hold_days == 126
    assert s.stop_price < s.entry_price
    assert s.setup_grade == "A"                     # high conviction


def test_replay_no_events_for_ticker():
    df = _ohlcv()
    state = K.EventState({"OTHER": [_ev("OTHER", df.index[50].date().isoformat())]})
    assert K.replay(df, "ABC", {}, state) == []


def test_replay_overlap_suppression():
    df = _ohlcv(n=400)
    d1 = df.index[50].date()
    d2 = df.index[80].date()    # within 126-bar hold of d1 → suppressed
    d3 = df.index[300].date()   # well after → fires
    state = K.EventState({"ABC": [
        _ev("ABC", d1.isoformat()), _ev("ABC", d2.isoformat()), _ev("ABC", d3.isoformat())]})
    sigs = K.replay(df, "ABC", {"max_hold_days": 126}, state)
    assert len(sigs) == 2
    # entry_date == the (signal) event date; d2 must be suppressed
    assert {s.entry_date for s in sigs} == {d1, d3}
    # each fill is strictly after its event date (next-bar entry)
    for s in sigs:
        assert s.fill_date > s.entry_date


def test_replay_event_after_data_end_skipped():
    df = _ohlcv(n=60)
    ev_date = df.index[-1].date().isoformat()   # no bar after → no entry
    state = K.EventState({"ABC": [_ev("ABC", ev_date)]})
    assert K.replay(df, "ABC", {}, state) == []


def test_replay_event_on_weekend_enters_next_trading_bar():
    df = _ohlcv()
    # pick a Friday past the ATR warmup (>=30 bars), event on the Saturday →
    # entry should be the following Monday bar
    fri = None
    for i, ix in enumerate(df.index):
        if i >= 30 and ix.weekday() == 4:   # Friday
            fri = (i, ix)
            break
    assert fri is not None
    i, ix = fri
    sat = (ix + pd.Timedelta(days=1)).date()
    state = K.EventState({"ABC": [_ev("ABC", sat.isoformat())]})
    sigs = K.replay(df, "ABC", {}, state)
    assert len(sigs) == 1
    assert sigs[0].fill_date == df.index[i + 1].date()   # next trading bar (Mon)


def test_precompute_requires_events_path():
    with pytest.raises(ValueError, match="events_path"):
        K.precompute({"ABC": _ohlcv()}, {})


def test_precompute_loads_and_filters(tmp_path):
    path = tmp_path / "events.yml"
    write_events(path, [_ev("ABC", "2024-03-15"), _ev("OOU", "2024-03-16")], meta={})
    # universe excludes OOU and the benchmark
    state = K.precompute({"ABC": None, "SPY": None},
                         {"events_path": str(path), "benchmark": "SPY"})
    assert set(state.events_by_ticker.keys()) == {"ABC"}


# ---- build_events composition (injected, no network) --------------------


def test_build_events_composes_pipeline():
    # one ticker, two insiders buying within the cluster window
    purchases = [
        SimpleNamespace(ticker="ABC", insider_cik="1", shares=300, value=30000,
                        event_date="2024-03-15", is_officer=True, is_director=False,
                        is_ten_pct_owner=False, officer_title="CEO", insider_name="A"),
        SimpleNamespace(ticker="ABC", insider_cik="2", shares=300, value=30000,
                        event_date="2024-03-16", is_officer=True, is_director=False,
                        is_ten_pct_owner=False, officer_title="CFO", insider_name="B"),
    ]

    def ingest_day_fn(d):
        return purchases if d == "2024-03-15" else []

    def classify_fn(cik, asof):
        return SimpleNamespace(tier="elite")

    def shares_fn(ticker, asof):
        return 1_000_000   # 600/1e6 = 0.06% → HIGH size

    events = build_events(
        ["ABC", "ZZZ"], date(2024, 3, 15), date(2024, 3, 16),
        ingest_day_fn=ingest_day_fn, classify_fn=classify_fn,
        shares_outstanding_fn=shares_fn, cluster_window_days=2,
    )
    assert len(events) == 1
    e = events[0]
    assert e.ticker == "ABC"
    assert e.n_insiders == 2
    assert e.conviction_level == "high"
    assert e.best_tier == "elite"


def test_build_events_drops_below_min_conviction():
    purchases = [
        SimpleNamespace(ticker="ABC", insider_cik="1", shares=10, value=100,
                        event_date="2024-03-15", is_officer=True, is_director=False,
                        is_ten_pct_owner=False, officer_title="CEO", insider_name="A"),
    ]
    events = build_events(
        ["ABC"], date(2024, 3, 15), date(2024, 3, 15),
        ingest_day_fn=lambda d: purchases,
        classify_fn=lambda cik, asof: SimpleNamespace(tier="poor"),
        shares_outstanding_fn=lambda t, a: 1_000_000_000,  # negligible size
        min_conviction="medium",
    )
    assert events == []   # poor tier + negligible size → LOW < medium
