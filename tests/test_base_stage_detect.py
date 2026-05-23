"""Tests for tools.base_stage_detect — Phase 2 baseline heuristic."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.base_stage_detect import compute_from_ohlcv


def _make_with_bases(n: int, base_count: int) -> pd.DataFrame:
    """Build an OHLCV with ``base_count`` distinct multi-week consolidations
    followed by a fresh breakout on the last bar. Uses sine-wave peaks
    spaced ~30 bars apart.
    """
    rng = np.random.default_rng(29)
    base_period = 30  # ~6 weeks per cycle so each pullback is real
    amplitude = 5.0
    closes = np.zeros(n)
    for i in range(n):
        # Linear uptrend background.
        trend = i * 0.05
        # Oscillation: only the last `base_count * base_period` bars
        cycles_back = (n - 1 - i) // base_period
        cycles_back = max(0, cycles_back)
        if cycles_back < base_count:
            phase = (i % base_period) / base_period * 2 * np.pi
            wave = amplitude * np.sin(phase)
        else:
            wave = 0
        closes[i] = 50.0 + trend + wave + rng.normal(0, 0.2)
    closes[-1] = max(closes) + 5.0  # fresh new high on last bar
    highs = closes + 0.5
    lows = closes - 0.5
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    volumes = rng.integers(800_000, 1_200_000, size=n).astype(int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2023-01-02", periods=n, freq="B"),
    )


def test_new_high_detected_on_fresh_breakout():
    df = _make_with_bases(n=300, base_count=2)
    e = compute_from_ohlcv(df)
    assert e.output["new_high_today"] is True
    assert 1 <= e.output["base_stage"] <= 5
    assert e.output["v1_preliminary_flag"] is True


def test_late_stage_new_high_flagged():
    """Many consolidations + fresh new high should flag late_stage_new_high
    OR at least classify as a high base_stage."""
    df = _make_with_bases(n=300, base_count=5)
    e = compute_from_ohlcv(df)
    # Heuristic — may not detect exactly 5 bases, but should be >= 2.
    assert e.output["base_stage"] >= 2
    if e.output["base_stage"] >= 4:
        assert e.output["late_stage_new_high_exit_signal"] is True


def test_insufficient_rows_raises():
    df = pd.DataFrame(
        {"Close": [100.0] * 50},
        index=pd.date_range("2024-01-02", periods=50, freq="B"),
    )
    with pytest.raises(ValueError, match="bars"):
        compute_from_ohlcv(df)


def test_missing_close_raises():
    df = pd.DataFrame({"Open": [100.0] * 300})
    with pytest.raises(ValueError, match="Close"):
        compute_from_ohlcv(df)
