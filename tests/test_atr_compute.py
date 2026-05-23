"""Tests for tools.atr_compute."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from tools.atr_compute import compute_from_ohlcv


def test_atr_constant_range():
    """If every bar has the same true range, ATR converges to that range."""
    n = 50
    df = pd.DataFrame(
        {
            "High": [101.0] * n,
            "Low": [99.0] * n,
            "Close": [100.0] * n,
        },
        index=pd.date_range("2024-01-02", periods=n, freq="B"),
    )
    e = compute_from_ohlcv(df, period=14)
    # TR_t = max(H-L=2, |H-prevClose|=1, |L-prevClose|=1) = 2 for all t
    assert math.isclose(e.output["atr"], 2.0, abs_tol=1e-9)
    assert e.output["period"] == 14


def test_atr_pct_of_close():
    n = 50
    df = pd.DataFrame(
        {
            "High": [110.0] * n,
            "Low": [90.0] * n,
            "Close": [100.0] * n,
        },
        index=pd.date_range("2024-01-02", periods=n, freq="B"),
    )
    e = compute_from_ohlcv(df, period=14)
    assert math.isclose(e.output["atr"], 20.0, abs_tol=1e-9)
    assert math.isclose(e.output["atr_pct_of_close"], 0.20, abs_tol=1e-9)


def test_missing_columns_raises():
    df = pd.DataFrame({"Close": [100, 101, 102]})
    with pytest.raises(ValueError, match="High"):
        compute_from_ohlcv(df)


def test_insufficient_rows_raises():
    df = pd.DataFrame(
        {"High": [1.0] * 10, "Low": [1.0] * 10, "Close": [1.0] * 10},
        index=pd.date_range("2024-01-02", periods=10, freq="B"),
    )
    with pytest.raises(ValueError, match="ATR"):
        compute_from_ohlcv(df, period=14)


def test_trace_entry_shape(uptrend_ohlcv):
    e = compute_from_ohlcv(uptrend_ohlcv, period=14)
    assert e.tool == "tools/atr_compute.py"
    assert e.output["period"] == 14
    assert e.output["atr"] > 0
    assert 0 < e.output["atr_pct_of_close"] < 1.0
