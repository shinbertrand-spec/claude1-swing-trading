"""Tests for the time-series-momentum (TSMOM) kind plugin.

Validates:
* `KIND` registration
* `precompute()` returns None (per-ticker only)
* `replay()` emits signal when trailing return positive at rebalance dates
* `replay()` emits no signal for sustained downtrend (negative trailing return)
* Rebalance cadence: signals spaced ~rebalance_period_days apart
* `replay()` skips benchmark
* Insufficient history returns no signals
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._kinds.ts_momentum import (
    KIND,
    precompute,
    replay,
)


def _df_steady_uptrend(n: int, start: float = 100.0, drift: float = 0.002) -> pd.DataFrame:
    closes = np.array([start * np.exp(drift * i) for i in range(n)])
    opens = closes.copy()
    highs = closes * 1.005
    lows = closes * 0.995
    volumes = np.full(n, 1_000_000, dtype=int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2022-01-03", periods=n, freq="B"),
    )


def _df_steady_downtrend(n: int, start: float = 100.0, drift: float = -0.002) -> pd.DataFrame:
    closes = np.array([start * np.exp(drift * i) for i in range(n)])
    opens = closes.copy()
    highs = closes * 1.005
    lows = closes * 0.995
    volumes = np.full(n, 1_000_000, dtype=int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2022-01-03", periods=n, freq="B"),
    )


def _params(lookback: int = 126) -> dict:
    return {
        "benchmark": "SPY",
        "lookback_days": lookback,
        "rebalance_period_days": 21,
        "max_hold_days": 21,
        "atr_period": 20,
        "atr_stop_multiple": 3.0,
        "risk_per_trade": 0.01,
    }


def test_kind_registry_has_strategy():
    assert KIND == "ts_momentum"
    assert KIND in KIND_REGISTRY


def test_precompute_returns_none():
    assert precompute({}, {}) is None


def test_replay_emits_signal_on_sustained_uptrend():
    df = _df_steady_uptrend(400)  # long enough for 126d lookback + multiple rebalances
    signals = replay(df, "X", _params(lookback=126), None)
    assert len(signals) >= 1
    for s in signals:
        assert s.setup_type == KIND
        assert s.stop_price < s.entry_price
        assert s.fill_date > s.entry_date
        assert s.notes["trailing_return"] > 0


def test_replay_no_signal_on_sustained_downtrend():
    df = _df_steady_downtrend(400)
    signals = replay(df, "X", _params(lookback=126), None)
    assert signals == []


def test_replay_rebalance_cadence():
    """Signals should be ~rebalance_period_days apart in bar-index terms."""
    df = _df_steady_uptrend(500)
    signals = replay(df, "X", _params(lookback=126), None)
    assert len(signals) >= 2
    # Signal dates should be at least rebalance_period_days apart.
    dates = [s.entry_date for s in signals]
    for prev, nxt in zip(dates, dates[1:]):
        gap_days = (nxt - prev).days
        # Business-day rebalance of 21 bars ≈ 29 calendar days; allow margin.
        assert gap_days >= 21


def test_replay_skips_benchmark():
    df = _df_steady_uptrend(400)
    params = _params()
    params["benchmark"] = "X"
    assert replay(df, "X", params, None) == []


def test_replay_insufficient_history():
    df = _df_steady_uptrend(50)  # < lookback + 2
    assert replay(df, "X", _params(lookback=126), None) == []


def test_replay_emits_no_signal_when_lookback_return_zero_or_negative():
    """Edge: trailing return == 0 → no signal (strict > 0 check)."""
    # Flat price → trailing return == 0
    closes = np.full(400, 100.0)
    opens = closes.copy()
    highs = closes * 1.005
    lows = closes * 0.995
    volumes = np.full(400, 1_000_000, dtype=int)
    df = pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2022-01-03", periods=400, freq="B"),
    )
    signals = replay(df, "X", _params(lookback=126), None)
    assert signals == []
