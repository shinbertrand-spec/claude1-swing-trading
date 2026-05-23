"""Tests for tools.climax_top_detect."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.climax_top_detect import compute_from_ohlcv


def _parabolic_top_df(n: int = 60) -> pd.DataFrame:
    """Synthetic parabolic blow-off: flat → sharp accelerating advance → today.

    Engineered to fire most climax-top patterns simultaneously.
    """
    rng = np.random.default_rng(17)
    base = [50.0] * (n - 15)
    # Accelerating rally over last 15 bars from 50 to ~75 with mostly up days.
    rally = list(np.linspace(50.5, 70.0, 12)) + [73.0, 74.5, 78.0]
    closes = np.array(base + rally, dtype=float)
    opens = np.concatenate([[50.0], closes[:-1]])
    highs = closes + 0.5
    # Today: largest spread.
    highs[-1] = closes[-1] + 3.0
    lows = closes - 0.5
    volumes = rng.integers(500_000, 700_000, size=n).astype(int)
    volumes[-1] = 5_000_000  # highest-ever volume today
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2024-01-02", periods=n, freq="B"),
    )


def _calm_df(n: int = 60) -> pd.DataFrame:
    """Truly calm: today's bar is the median, not extreme on any axis.
    Ensures the "largest"/"widest"/"highest" patterns don't spuriously fire.
    """
    rng = np.random.default_rng(19)
    closes = np.array([100.0 + rng.normal(0, 0.3) for _ in range(n)], dtype=float)
    # Force today to be at the median price so no "largest up day" or "widest spread"
    # spuriously fires; volume is also explicitly set to the median.
    closes[-1] = float(np.median(closes))
    closes[-2] = closes[-1] - 0.05  # tiny up move today
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    highs = closes + 0.3
    lows = closes - 0.3
    volumes = rng.integers(500_000, 800_000, size=n).astype(int)
    volumes[-1] = int(np.median(volumes))  # today's vol = median, not max
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2024-01-02", periods=n, freq="B"),
    )


def test_parabolic_top_fires_multiple_patterns():
    df = _parabolic_top_df()
    e = compute_from_ohlcv(df)
    out = e.output
    assert out["patterns_firing"] >= 3
    # Most expected patterns:
    assert out["patterns"]["sharp_advance"] is True
    assert out["patterns"]["highest_ever_volume"] is True
    assert out["v1_preliminary_flag"] is True


def test_calm_data_fires_few_patterns():
    df = _calm_df()
    e = compute_from_ohlcv(df)
    assert e.output["patterns_firing"] <= 1


def test_missing_columns_raises():
    df = pd.DataFrame({"Close": [100.0] * 30})
    with pytest.raises(ValueError, match="missing"):
        compute_from_ohlcv(df)


def test_insufficient_rows_raises():
    df = pd.DataFrame(
        {
            "Open": [1.0] * 5,
            "High": [1.0] * 5,
            "Low": [1.0] * 5,
            "Close": [1.0] * 5,
            "Volume": [1] * 5,
        },
        index=pd.date_range("2024-01-02", periods=5, freq="B"),
    )
    with pytest.raises(ValueError, match="bars"):
        compute_from_ohlcv(df)
