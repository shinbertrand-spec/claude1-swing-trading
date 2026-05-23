"""Tests for tools.ep_detect."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tools.ep_detect import compute_from_daily_ohlcv


def _make_daily(n: int = 30, base_price: float = 100.0, base_volume: int = 1_000_000):
    """Steady-price baseline + customizable last bar."""
    rng = np.random.default_rng(7)
    closes = np.array([base_price] * n, dtype=float)
    highs = closes * 1.005
    lows = closes * 0.995
    opens = closes.copy()
    volumes = rng.integers(int(base_volume * 0.9), int(base_volume * 1.1), size=n).astype(int)
    idx = pd.date_range("2024-01-02", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=idx,
    )


def test_gap_pct_arithmetic():
    df = _make_daily(n=30, base_price=100.0)
    # Today: gap up to $114, close at $116, high $118.
    df.iloc[-1, df.columns.get_loc("Open")] = 114.0
    df.iloc[-1, df.columns.get_loc("Close")] = 116.0
    df.iloc[-1, df.columns.get_loc("High")] = 118.0
    df.iloc[-1, df.columns.get_loc("Low")] = 113.0
    df.iloc[-1, df.columns.get_loc("Volume")] = 5_000_000
    e = compute_from_daily_ohlcv(df)
    assert math.isclose(e.output["gap_pct"], 0.14, rel_tol=1e-9)
    assert e.output["gap_band"] == "sweet_10_to_19"
    assert e.output["sweet_spot"] is True
    assert e.output["ep_eligible"] is True
    assert e.output["large_gap_risk"] is False
    # Intraday expansion = (118 - 114) / 114
    assert math.isclose(e.output["intraday_expansion_pct"], 4 / 114, rel_tol=1e-9)
    assert e.output["volume_today_vs_adv"] > 4.0


def test_below_threshold_not_eligible():
    df = _make_daily(n=30, base_price=100.0)
    df.iloc[-1, df.columns.get_loc("Open")] = 103.0
    df.iloc[-1, df.columns.get_loc("Close")] = 104.0
    df.iloc[-1, df.columns.get_loc("High")] = 105.0
    e = compute_from_daily_ohlcv(df)
    assert e.output["ep_eligible"] is False
    assert e.output["gap_band"] == "below_threshold"


def test_large_gap_flagged():
    df = _make_daily(n=30, base_price=100.0)
    df.iloc[-1, df.columns.get_loc("Open")] = 125.0
    df.iloc[-1, df.columns.get_loc("Close")] = 130.0
    df.iloc[-1, df.columns.get_loc("High")] = 132.0
    e = compute_from_daily_ohlcv(df)
    assert e.output["gap_band"] == "large_20_plus"
    assert e.output["large_gap_risk"] is True
    assert e.output["ep_eligible"] is True


def test_small_gap_band():
    df = _make_daily(n=30, base_price=100.0)
    df.iloc[-1, df.columns.get_loc("Open")] = 107.0
    df.iloc[-1, df.columns.get_loc("Close")] = 108.0
    df.iloc[-1, df.columns.get_loc("High")] = 109.0
    e = compute_from_daily_ohlcv(df)
    assert e.output["gap_band"] == "small_5_to_9"
    assert e.output["sweet_spot"] is False
    assert e.output["ep_eligible"] is False  # 7% < 10% floor


def test_intraday_volume_signals_when_provided():
    daily = _make_daily(n=30, base_price=100.0)
    daily.iloc[-1, daily.columns.get_loc("Open")] = 114.0
    daily.iloc[-1, daily.columns.get_loc("Close")] = 116.0
    daily.iloc[-1, daily.columns.get_loc("High")] = 118.0
    daily.iloc[-1, daily.columns.get_loc("Volume")] = 5_000_000

    # Build a 1-min intraday DataFrame for "today".
    import datetime as _dt

    today = pd.Timestamp("2024-02-09")  # arbitrary business day
    # 90 minutes from 09:00 to 10:30 ET — premarket (09:00-09:29) + open-30 (09:30-09:59).
    times = [today.replace(hour=9, minute=m) for m in range(0, 60)] + [
        today.replace(hour=10, minute=m) for m in range(0, 30)
    ]
    intraday = pd.DataFrame(
        {"Volume": [10_000] * len(times)},
        index=pd.DatetimeIndex(times),
    )
    e = compute_from_daily_ohlcv(daily, intraday_df=intraday)
    assert e.output["intraday_data_available"] is True
    # 30 minutes premarket (09:00-09:29) × 10k = 300k
    assert e.output["premarket_volume_shares"] == 30 * 10_000
    # 30 minutes first half hour (09:30-09:59) × 10k = 300k
    assert e.output["first_30min_volume"] == 30 * 10_000


def test_missing_columns_raises():
    df = pd.DataFrame({"Close": [100.0] * 30})
    with pytest.raises(ValueError, match="missing"):
        compute_from_daily_ohlcv(df)


def test_insufficient_rows_raises():
    df = _make_daily(n=10)
    with pytest.raises(ValueError, match="daily bars"):
        compute_from_daily_ohlcv(df)
