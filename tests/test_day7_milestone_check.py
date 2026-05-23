"""Tests for tools.day7_milestone_check."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.day7_milestone_check import compute_from_ohlcv


def _make_post_entry_df(
    pre_n: int = 20,
    post_closes: list[float] | None = None,
    post_lows: list[float] | None = None,
    entry_low: float = 100.0,
    pre_price: float = 95.0,
):
    """Build a DataFrame with ``pre_n`` pre-entry bars at pre_price,
    then an entry bar at price 105 (low=entry_low), then ``len(post_closes)``
    post-entry bars with specified closes + lows.
    """
    post_closes = post_closes or []
    post_lows = post_lows or post_closes

    closes_pre = [pre_price] * pre_n
    closes = closes_pre + [105.0] + list(post_closes)
    n = len(closes)
    highs = [c + 1.0 for c in closes]
    lows_pre = [pre_price - 0.5] * pre_n
    lows = lows_pre + [entry_low] + list(post_lows)
    opens = closes  # not used by the check
    volumes = [1_000_000] * n
    idx = pd.date_range("2024-01-02", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=idx,
    )


def test_survives_with_all_strong_days():
    """Post-entry closes 106, 107, ..., 112 — never broke entry low, always
    above the 10-MA (which is dragging up from pre_price=95)."""
    df = _make_post_entry_df(
        pre_n=20,
        post_closes=[106, 107, 108, 109, 110, 111, 112],
        post_lows=[105.5] * 7,  # always above entry_low=100
        entry_low=100.0,
        pre_price=95.0,
    )
    entry_date = pd.Timestamp(df.index[20]).date()
    e = compute_from_ohlcv(df, entry_date=entry_date, entry_low=100.0)
    assert e.output["survives_day7"] is True
    assert e.output["broke_entry_low"] is False
    assert e.output["closed_below_10ma"] is False
    assert e.output["fully_evaluated"] is True
    assert e.output["trading_days_since_entry"] == 7


def test_fails_when_breaks_entry_low_intraday():
    """Day-3 intraday low dips below entry low → fails."""
    df = _make_post_entry_df(
        pre_n=20,
        post_closes=[106, 107, 108, 109, 110, 111, 112],
        post_lows=[105.5, 105.5, 99.0, 105.5, 105.5, 105.5, 105.5],
        entry_low=100.0,
    )
    entry_date = pd.Timestamp(df.index[20]).date()
    e = compute_from_ohlcv(df, entry_date=entry_date, entry_low=100.0)
    assert e.output["survives_day7"] is False
    assert e.output["broke_entry_low"] is True
    assert e.output["broke_entry_low_on_day"] == 3


def test_close_low_only_mode_ignores_intraday_dip():
    """Same Day-3 intraday dip but closes above entry low → close-only mode passes."""
    df = _make_post_entry_df(
        pre_n=20,
        post_closes=[106, 107, 108, 109, 110, 111, 112],
        post_lows=[105.5, 105.5, 99.0, 105.5, 105.5, 105.5, 105.5],
        entry_low=100.0,
    )
    entry_date = pd.Timestamp(df.index[20]).date()
    e = compute_from_ohlcv(
        df, entry_date=entry_date, entry_low=100.0, intraday_low_check=False
    )
    assert e.output["broke_entry_low"] is False  # close (108) above entry_low (100)
    assert e.output["survives_day7"] is True


def test_fails_when_closes_below_10ma():
    """A late post-entry plunge below 10-MA fails the trail check."""
    # Pre-entry at 95 for 20 bars; entry at 105; post-entry recovers then plunges.
    df = _make_post_entry_df(
        pre_n=20,
        post_closes=[110, 112, 114, 90, 92, 94, 96],
        post_lows=[109, 111, 113, 89, 91, 93, 95],
        entry_low=100.0,
    )
    entry_date = pd.Timestamp(df.index[20]).date()
    e = compute_from_ohlcv(df, entry_date=entry_date, entry_low=100.0)
    # Day 4 close=90; 10-MA over bars 11..20 includes some pre-entry 95s + post-entry
    # 105,110,112,114,90; MA roughly ~ (95*6 + 105 + 110 + 112 + 90)/10 = 98.7
    # so 90 < 98.7 = close below 10-MA → fail.
    assert e.output["closed_below_10ma"] is True
    assert e.output["survives_day7"] is False


def test_partial_evaluation_not_yet_milestone():
    """Only 3 post-entry bars → not fully evaluated, survives_day7 False."""
    df = _make_post_entry_df(
        pre_n=20,
        post_closes=[106, 107, 108],
        post_lows=[105, 106, 107],
        entry_low=100.0,
    )
    entry_date = pd.Timestamp(df.index[20]).date()
    e = compute_from_ohlcv(df, entry_date=entry_date, entry_low=100.0)
    assert e.output["fully_evaluated"] is False
    assert e.output["survives_day7"] is False
    assert e.output["trading_days_since_entry"] == 3


def test_entry_date_not_in_index_raises():
    df = _make_post_entry_df(
        pre_n=20, post_closes=[106, 107, 108, 109, 110, 111, 112]
    )
    with pytest.raises(ValueError, match="entry_date"):
        compute_from_ohlcv(df, entry_date="2099-01-01", entry_low=100.0)


def test_too_few_pre_entry_bars_raises():
    """Need ≥10 pre-entry bars for the 10-MA seed."""
    df = _make_post_entry_df(
        pre_n=5, post_closes=[106, 107, 108], post_lows=[105, 106, 107]
    )
    entry_date = pd.Timestamp(df.index[5]).date()
    with pytest.raises(ValueError, match="bars before entry"):
        compute_from_ohlcv(df, entry_date=entry_date, entry_low=100.0)
