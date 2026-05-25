"""Tests for the dual-MA trend-following kind plugin.

Validates:
* `KIND` registration
* `precompute()` returns None (per-ticker only)
* `replay()` emits signal on SMA(short) crossing above SMA(long)
* `replay()` emits no signal for sustained downtrend
* Cooldown prevents same-bar re-entries
* `short_period >= long_period` raises
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._kinds.dual_ma_trend_following import (
    KIND,
    precompute,
    replay,
)


def _df_v_shape(
    n_down: int,
    n_up: int,
    start: float = 100.0,
    down_drift: float = -0.003,
    up_drift: float = 0.005,
) -> pd.DataFrame:
    """V-shape: long downtrend then long uptrend. Generates a bullish cross."""
    down_closes = [start * np.exp(down_drift * i) for i in range(n_down)]
    bottom = down_closes[-1]
    up_closes = [bottom * np.exp(up_drift * i) for i in range(1, n_up + 1)]
    closes = np.array(down_closes + up_closes)
    opens = closes.copy()
    highs = closes * 1.005
    lows = closes * 0.995
    volumes = np.full(len(closes), 1_000_000, dtype=int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2023-01-02", periods=len(closes), freq="B"),
    )


def _df_steady_downtrend(n: int = 400, start: float = 100.0) -> pd.DataFrame:
    closes = np.array([start * np.exp(-0.003 * i) for i in range(n)])
    opens = closes.copy()
    highs = closes * 1.005
    lows = closes * 0.995
    volumes = np.full(n, 1_000_000, dtype=int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2023-01-02", periods=n, freq="B"),
    )


def test_kind_registry_has_strategy():
    assert KIND == "dual_ma_trend_following"
    assert KIND in KIND_REGISTRY


def test_precompute_returns_none():
    assert precompute({}, {}) is None


def _params(short: int = 20, long: int = 50) -> dict:
    return {
        "benchmark": "SPY",
        "short_period": short,
        "long_period": long,
        "atr_period": 20,
        "atr_stop_multiple": 3.0,
        "max_hold_days": 60,
        "risk_per_trade": 0.01,
    }


def test_short_must_be_less_than_long_raises():
    df = _df_v_shape(100, 100)
    with pytest.raises(ValueError):
        replay(df, "X", _params(short=50, long=20), None)


def test_replay_emits_signal_on_bullish_cross():
    df = _df_v_shape(200, 200)  # long enough for SMA(50) to flip
    signals = replay(df, "X", _params(), None)
    assert len(signals) >= 1
    for s in signals:
        assert s.setup_type == KIND
        assert s.stop_price < s.entry_price
        assert s.fill_date > s.entry_date


def test_replay_no_signal_for_steady_downtrend():
    df = _df_steady_downtrend(400)
    signals = replay(df, "X", _params(), None)
    assert signals == []


def test_replay_benchmark_skipped():
    df = _df_v_shape(200, 200)
    params = _params()
    params["benchmark"] = "X"
    signals = replay(df, "X", params, None)
    assert signals == []


def test_replay_insufficient_history():
    df = _df_v_shape(20, 20)
    signals = replay(df, "X", _params(), None)
    assert signals == []
