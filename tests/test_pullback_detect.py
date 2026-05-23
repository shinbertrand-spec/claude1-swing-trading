"""Tests for tools.pullback_detect."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.pullback_detect import compute_from_ohlcv


def _make_pullback_setup(triggers: bool = True) -> pd.DataFrame:
    """Stage 2 stock pulled back to its 20-SMA today with a bullish hammer."""
    n = 60
    rng = np.random.default_rng(31)
    # Uptrend baseline.
    closes = np.array([100.0 + i * 0.5 for i in range(n)], dtype=float)
    opens = closes.copy()
    highs = closes + 1.0
    lows = closes - 1.0
    volumes = rng.integers(900_000, 1_100_000, size=n).astype(int)

    sma20 = float(np.mean(closes[-20:]))

    if triggers:
        # Today: close ≈ SMA20, hammer pattern (long lower wick, body small
        # but big enough that the upper-wick:body ratio stays under threshold).
        closes[-1] = sma20 + 0.5      # body top (close above open = green hammer)
        opens[-1] = sma20 - 0.5       # body bottom (body length = 1.0)
        highs[-1] = sma20 + 0.55      # tiny upper wick (0.05 = 5% of body)
        lows[-1] = sma20 - 3.0        # long lower wick (2.5 = 2.5× body)
        volumes[-1] = 600_000          # below 20d avg
    else:
        closes[-1] = sma20 + 10.0     # nowhere near SMA20
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2024-01-02", periods=n, freq="B"),
    )


def test_pullback_with_hammer_triggers():
    df = _make_pullback_setup(triggers=True)
    e = compute_from_ohlcv(df)
    assert e.output["detected"] is True
    assert e.output["candle_type"] == "hammer"
    assert e.output["criteria"]["near_20sma"] is True
    assert e.output["criteria"]["declining_volume_on_pullback"] is True


def test_no_pullback_when_extended():
    df = _make_pullback_setup(triggers=False)
    e = compute_from_ohlcv(df)
    assert e.output["detected"] is False
    assert e.output["criteria"]["near_20sma"] is False


def test_missing_columns_raises():
    df = pd.DataFrame({"Close": [100.0] * 30})
    with pytest.raises(ValueError, match="missing"):
        compute_from_ohlcv(df)


def test_insufficient_rows_raises():
    df = pd.DataFrame(
        {
            "Open": [1.0] * 10,
            "High": [1.0] * 10,
            "Low": [1.0] * 10,
            "Close": [1.0] * 10,
            "Volume": [1] * 10,
        },
        index=pd.date_range("2024-01-02", periods=10, freq="B"),
    )
    with pytest.raises(ValueError, match="bars"):
        compute_from_ohlcv(df)
