"""Tests for tools.sltb_scan."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.sltb_scan import compute_from_ohlcv


def _make_uptrend_sltb_setup(triggers: bool = True) -> pd.DataFrame:
    """Build OHLCV with steady uptrend baseline + customizable last bar.

    If triggers=True, the last bar satisfies all 6 SLTB criteria.
    """
    n = 80
    rng = np.random.default_rng(11)
    # Build a slow steady uptrend so 7-MA > 65-MA by >5%.
    closes = np.array([100.0 + i * 0.5 for i in range(n)], dtype=float)
    opens = np.roll(closes, 1)
    opens[0] = 100.0
    highs = closes + 0.4
    lows = closes - 0.4
    volumes = rng.integers(500_000, 800_000, size=n).astype(int)

    if triggers:
        # Yesterday: small gain ~0.4% (50.5 -> 50.7 ish — we'll force the values).
        # Engineer the last 3 bars to satisfy the rules:
        # bar -3 close = 138, bar -2 close = 138.7 (yest gain = 0.5%), bar -1 close = 141 (today gain = 1.66%)
        # ma_7 over last 7 ≈ (138+138.7+141 + earlier ones)/7; ma_65 ≈ 116 → ratio ~0.21 way over 5%
        closes[-3] = 138.0
        closes[-2] = 138.7
        closes[-1] = 141.0
        opens[-1] = 138.8     # close > open
        opens[-2] = 138.0
        highs[-1] = 141.5
        lows[-1] = 138.0
        # 3-day min volume needs to be >= 100k — already is (500-800k).
    else:
        # Force today_close == today_open == yest_close (no gain criteria fails)
        closes[-1] = closes[-2]
        opens[-1] = closes[-1]

    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2024-01-02", periods=n, freq="B"),
    )


def test_sltb_triggers_on_engineered_setup():
    df = _make_uptrend_sltb_setup(triggers=True)
    e = compute_from_ohlcv(df)
    out = e.output
    assert out["sltb_triggered"] is True
    assert all(out["criteria"].values())


def test_sltb_no_trigger_when_no_gain():
    df = _make_uptrend_sltb_setup(triggers=False)
    e = compute_from_ohlcv(df)
    assert e.output["sltb_triggered"] is False


def test_penny_stock_fails():
    df = _make_uptrend_sltb_setup(triggers=True)
    # Scale all prices down so today's close < $3.
    df = df * 0.01
    df["Volume"] = (df["Volume"] * 100).astype(int)  # keep volume integer
    e = compute_from_ohlcv(df)
    assert e.output["criteria"]["close_at_least_3_dollars"] is False
    assert e.output["sltb_triggered"] is False


def test_low_volume_fails():
    df = _make_uptrend_sltb_setup(triggers=True)
    df["Volume"] = 50_000  # below 100k threshold
    e = compute_from_ohlcv(df)
    assert e.output["criteria"]["min_3d_volume_100k"] is False
    assert e.output["sltb_triggered"] is False


def test_insufficient_rows_raises():
    df = pd.DataFrame(
        {"Open": [1.0] * 30, "Close": [1.0] * 30, "Volume": [100_000] * 30}
    )
    with pytest.raises(ValueError, match="bars"):
        compute_from_ohlcv(df)


def test_missing_columns_raises():
    df = pd.DataFrame({"Close": [100.0] * 100})
    with pytest.raises(ValueError, match="missing"):
        compute_from_ohlcv(df)
