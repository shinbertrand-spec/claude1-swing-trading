"""Tests for the event_earnings_drift KIND (qualification, entry alignment, overlap)."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from tools.fundamentals.earnings_events import EarningsEvent, write_events
from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._kinds import event_earnings_drift as K


# ---- registration --------------------------------------------------------


def test_kind_registered():
    assert K.KIND == "event_earnings_drift"
    assert KIND_REGISTRY[K.KIND] is K


def test_kind_is_momentum_class_for_fills():
    from tools.auto_paper.entry_pricing import MOMENTUM_KINDS
    assert "event_earnings_drift" in MOMENTUM_KINDS


# ---- fixtures ------------------------------------------------------------


def _df(n=60, start=date(2024, 1, 2)):
    """Synthetic weekday OHLCV frame."""
    dates, d = [], start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    idx = pd.DatetimeIndex([pd.Timestamp(x) for x in dates])
    base = pd.Series(range(n), index=idx).astype(float)
    return pd.DataFrame({
        "Open": 100.0 + base * 0.1,
        "High": 101.0 + base * 0.1,
        "Low": 99.0 + base * 0.1,
        "Close": 100.5 + base * 0.1,
    }, index=idx)


def _event(ticker, rd, *, ear=0.05, rank=95.0, hist=3):
    return EarningsEvent(ticker=ticker, announce_date=rd, timing="amc",
                         reaction_date=rd, ear=ear, ear_rank_pct=rank,
                         prior_pos_ear_count=hist)


def _state(events, tmp_path, universe=("ABC",)):
    path = tmp_path / "events.yml"
    write_events(path, events, meta={})
    dfs = {t: _df() for t in universe}
    return K.precompute(dfs, {"events_path": str(path), "benchmark": "SPY"})


PARAMS = {"ear_top_pct": 10, "history_condition": True, "max_hold_days": 10,
          "atr_period": 20, "atr_stop_multiple": 3.0}


# ---- entry alignment -----------------------------------------------------


def test_entry_is_first_bar_strictly_after_reaction_date(tmp_path):
    df = _df()
    rd = df.index[30].date().isoformat()
    state = _state([_event("ABC", rd)], tmp_path)
    sigs = K.replay(df, "ABC", PARAMS, state)
    assert len(sigs) == 1
    assert sigs[0].fill_date == df.index[31].date()          # strictly after
    assert sigs[0].entry_price == pytest.approx(float(df.iloc[31]["Open"]))
    assert sigs[0].entry_date == df.index[30].date()


def test_reaction_on_last_bar_yields_no_signal(tmp_path):
    df = _df()
    rd = df.index[-1].date().isoformat()
    state = _state([_event("ABC", rd)], tmp_path)
    assert K.replay(df, "ABC", PARAMS, state) == []


# ---- §3c qualification ---------------------------------------------------


def test_negative_or_null_ear_skipped(tmp_path):
    df = _df()
    rd = df.index[30].date().isoformat()
    state = _state([_event("ABC", rd, ear=-0.02), _event("ABC", rd, ear=None)], tmp_path)
    assert K.replay(df, "ABC", PARAMS, state) == []


def test_rank_floor_enforced(tmp_path):
    df = _df()
    rd = df.index[30].date().isoformat()
    state = _state([_event("ABC", rd, rank=89.9)], tmp_path)   # top-10 needs >= 90
    assert K.replay(df, "ABC", PARAMS, state) == []
    state = _state([_event("ABC", rd, rank=90.0)], tmp_path)
    assert len(K.replay(df, "ABC", PARAMS, state)) == 1


def test_unranked_event_unusable(tmp_path):
    df = _df()
    rd = df.index[30].date().isoformat()
    state = _state([_event("ABC", rd, rank=None)], tmp_path)
    assert K.replay(df, "ABC", PARAMS, state) == []


def test_history_condition_toggle(tmp_path):
    df = _df()
    rd = df.index[30].date().isoformat()
    state = _state([_event("ABC", rd, hist=1)], tmp_path)      # < 2 of last 4
    assert K.replay(df, "ABC", PARAMS, state) == []
    off = dict(PARAMS, history_condition=False)
    assert len(K.replay(df, "ABC", off, state)) == 1


# ---- overlap suppression + stop ------------------------------------------


def test_overlap_suppression_one_position_per_name(tmp_path):
    df = _df()
    rd1 = df.index[30].date().isoformat()
    rd2 = df.index[34].date().isoformat()   # inside the 10-day hold window
    rd3 = df.index[45].date().isoformat()   # after the hold expires
    state = _state([_event("ABC", rd1), _event("ABC", rd2), _event("ABC", rd3)], tmp_path)
    sigs = K.replay(df, "ABC", PARAMS, state)
    assert [s.entry_date.isoformat() for s in sigs] == [rd1, rd3]


def test_stop_is_3x_atr_below_entry(tmp_path):
    df = _df()
    rd = df.index[30].date().isoformat()
    state = _state([_event("ABC", rd)], tmp_path)
    sig = K.replay(df, "ABC", PARAMS, state)[0]
    assert sig.stop_price == pytest.approx(sig.entry_price - 3.0 * sig.atr_at_signal)
    assert sig.target_price is None                      # the hold IS the exit
    assert sig.max_hold_days == 10
