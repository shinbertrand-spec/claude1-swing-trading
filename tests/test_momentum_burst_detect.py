"""Tests for tools.momentum_burst_detect."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tools.momentum_burst_detect import compute_from_ohlcv


def _make(n: int = 30, base_price: float = 100.0, base_volume: int = 1_000_000) -> pd.DataFrame:
    rng = np.random.default_rng(13)
    closes = np.array([base_price] * n, dtype=float)
    opens = closes.copy()
    highs = closes * 1.005
    lows = closes * 0.995
    volumes = rng.integers(int(base_volume * 0.9), int(base_volume * 1.1), size=n).astype(int)
    idx = pd.date_range("2024-01-02", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=idx,
    )


def test_daily_burst_triggers():
    """Today: +5% on 1.6× volume → daily burst."""
    df = _make()
    df.iloc[-1, df.columns.get_loc("Close")] = 105.0
    df.iloc[-1, df.columns.get_loc("Open")] = 101.0
    df.iloc[-1, df.columns.get_loc("Volume")] = 1_600_000
    e = compute_from_ohlcv(df)
    assert e.output["triggered"] is True
    assert e.output["daily_burst"] is True
    assert math.isclose(e.output["day_pct"], 0.05, rel_tol=1e-9)


def test_gap_burst_triggers():
    """Today: gap +6% but only +2% on close, 1.5× volume → gap burst."""
    df = _make()
    df.iloc[-1, df.columns.get_loc("Open")] = 106.0
    df.iloc[-1, df.columns.get_loc("Close")] = 102.0
    df.iloc[-1, df.columns.get_loc("Volume")] = 1_500_000
    e = compute_from_ohlcv(df)
    assert e.output["gap_burst"] is True
    assert e.output["triggered"] is True


def test_no_trigger_without_volume():
    """+5% but only 1.1× volume → no trigger (vol below 1.4× threshold)."""
    df = _make()
    df.iloc[-1, df.columns.get_loc("Close")] = 105.0
    df.iloc[-1, df.columns.get_loc("Open")] = 101.0
    df.iloc[-1, df.columns.get_loc("Volume")] = 1_100_000
    e = compute_from_ohlcv(df)
    assert e.output["triggered"] is False


def test_no_trigger_without_gain():
    """+1% on 2× volume → no trigger (gain below 4% threshold)."""
    df = _make()
    df.iloc[-1, df.columns.get_loc("Close")] = 101.0
    df.iloc[-1, df.columns.get_loc("Open")] = 100.5
    df.iloc[-1, df.columns.get_loc("Volume")] = 2_000_000
    e = compute_from_ohlcv(df)
    assert e.output["triggered"] is False


def test_missing_columns_raises():
    df = pd.DataFrame({"Close": [100.0] * 30})
    with pytest.raises(ValueError, match="missing"):
        compute_from_ohlcv(df)


def test_insufficient_rows_raises():
    df = _make(n=15)
    with pytest.raises(ValueError, match="bars"):
        compute_from_ohlcv(df)
