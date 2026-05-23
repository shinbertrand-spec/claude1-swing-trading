"""Tests for tools.violations_detect."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.violations_detect import compute_from_ohlcv


def _make(n: int = 80, post_entry_pattern: str = "calm") -> pd.DataFrame:
    """Build OHLCV with a controllable post-entry segment.

    Entry is 30 bars before the end. Pre-entry: steady uptrend baseline.
    Post-entry pattern: 'calm' / 'distribution' / 'breakdown'.
    """
    rng = np.random.default_rng(23)
    pre_n = n - 30
    pre_closes = np.array([100.0 + i * 0.1 for i in range(pre_n)], dtype=float)
    if post_entry_pattern == "calm":
        post_closes = np.array([100.0 + pre_n * 0.1 + i * 0.05 for i in range(30)])
        post_volumes = rng.integers(800_000, 1_200_000, size=30)
        post_highs = post_closes + 0.5
        post_lows = post_closes - 0.5
        post_opens = post_closes
    elif post_entry_pattern == "distribution":
        # Three consecutive lower lows on rising volume + more down than up days.
        post_closes = np.array([100.0 + pre_n * 0.1 - i * 0.4 for i in range(30)])
        post_volumes = np.array([1_500_000 + i * 50_000 for i in range(30)])
        # Engineer 3 consecutive lower lows on the last 3 bars.
        post_highs = post_closes + 0.5
        post_lows = post_closes - 0.5
        post_lows[-3] = post_lows[-4] - 0.5
        post_lows[-2] = post_lows[-3] - 0.5
        post_lows[-1] = post_lows[-2] - 0.5
        post_opens = post_closes + 0.3   # close < open: more bad closes
    else:  # breakdown — close below 20MA on heavy volume
        # Drop sharply at the end with massive volume.
        post_closes = np.array([100.0 + pre_n * 0.1] * 29 + [80.0])
        post_volumes = np.array([1_000_000] * 29 + [5_000_000])
        post_highs = post_closes + 0.5
        post_lows = post_closes - 0.5
        post_opens = post_closes

    closes = np.concatenate([pre_closes, post_closes])
    opens = np.concatenate([pre_closes, post_opens])
    highs = np.concatenate([pre_closes + 0.5, post_highs])
    lows = np.concatenate([pre_closes - 0.5, post_lows])
    volumes = np.concatenate([np.full(pre_n, 1_000_000), post_volumes]).astype(int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2024-01-02", periods=n, freq="B"),
    )


def test_calm_post_entry_no_violations():
    df = _make(post_entry_pattern="calm")
    entry_date = pd.Timestamp(df.index[-30]).date()
    e = compute_from_ohlcv(df, entry_date=entry_date)
    assert e.output["violations_firing"] == 0


def test_distribution_pattern_fires_violations():
    df = _make(post_entry_pattern="distribution")
    entry_date = pd.Timestamp(df.index[-30]).date()
    e = compute_from_ohlcv(df, entry_date=entry_date)
    # Expect at least three_lower_lows_on_volume and more_down_than_up to fire.
    assert e.output["violations"]["three_lower_lows_on_volume"] is True
    assert e.output["violations"]["more_down_than_up"] is True
    assert e.output["violations_firing"] >= 2


def test_breakdown_fires_violation_5():
    df = _make(post_entry_pattern="breakdown")
    entry_date = pd.Timestamp(df.index[-30]).date()
    e = compute_from_ohlcv(df, entry_date=entry_date)
    assert e.output["violations"]["close_below_20_or_50_MA_on_heavy_volume"] is True
    assert e.output["violation_5_alone_full_exit"] is True


def test_entry_date_not_in_index_raises():
    df = _make()
    with pytest.raises(ValueError, match="entry_date"):
        compute_from_ohlcv(df, entry_date="2099-01-01")


def test_missing_columns_raises():
    df = pd.DataFrame({"Close": [100.0] * 80})
    with pytest.raises(ValueError, match="missing"):
        compute_from_ohlcv(df, entry_date="2024-01-02")
