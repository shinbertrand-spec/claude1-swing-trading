"""Tests for the cross-sectional short-term reversal kind plugin.

Validates:
* `KIND` registration
* `_trailing_return` arithmetic
* `precompute()` bottom-N ranking
* `replay()` emits signals only when ticker in bottom-N
* Stop invariants
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._kinds.xs_short_term_reversal import (
    KIND,
    _trailing_return,
    precompute,
    replay,
)


def _df_constant_drift(n: int, start: float, drift: float) -> pd.DataFrame:
    """Build OHLCV with a constant per-bar log-drift."""
    closes = np.array([start * np.exp(drift * i) for i in range(n)])
    opens = closes.copy()
    highs = closes * 1.005
    lows = closes * 0.995
    volumes = np.full(n, 1_000_000, dtype=int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2023-01-02", periods=n, freq="B"),
    )


def test_kind_registry_has_strategy():
    assert KIND == "xs_short_term_reversal"
    assert KIND in KIND_REGISTRY


def test_trailing_return_insufficient_history():
    assert np.isnan(_trailing_return(pd.Series([100.0, 101.0]), lookback=5))


def test_trailing_return_known_window():
    closes = pd.Series([100.0, 102.0, 103.0, 104.0, 105.0, 110.0])
    # lookback=5 means use closes[-6:] = [100,102,103,104,105,110]; return = 110/100 - 1 = 0.10
    r = _trailing_return(closes, lookback=5)
    assert abs(r - 0.10) < 1e-9


def _params() -> dict:
    return {
        "benchmark": "SPY",
        "lookback_days": 5,
        "bottom_n": 2,
        "rebalance_period_days": 5,
        "max_hold_days": 5,
        "atr_period": 20,
        "atr_stop_multiple": 2.0,
        "risk_per_trade": 0.01,
    }


def test_precompute_ranks_biggest_losers_at_bottom():
    """A downtrend ticker should rank ahead of an uptrend ticker in bottom-N."""
    universe = {
        "SPY": _df_constant_drift(60, 100.0, 0.0),    # flat benchmark
        "UP": _df_constant_drift(60, 100.0, 0.005),   # uptrend
        "DOWN": _df_constant_drift(60, 100.0, -0.005),  # downtrend
        "FLAT": _df_constant_drift(60, 100.0, 0.0),
    }
    params = _params()
    params["bottom_n"] = 1
    state = precompute(universe, params)
    assert len(state.rebalance_dates) > 0
    # On any rebalance date with full history, DOWN should be in bottom-1.
    for d in state.rebalance_dates:
        if d in universe["DOWN"].index:
            picked = state.bottom_n_by_date[d]
            assert "DOWN" in picked
            assert "UP" not in picked


def test_replay_emits_signals_for_bottom_n_ticker():
    universe = {
        "SPY": _df_constant_drift(60, 100.0, 0.0),
        "DOWN": _df_constant_drift(60, 100.0, -0.005),
    }
    params = _params()
    params["bottom_n"] = 1
    state = precompute(universe, params)
    signals = replay(universe["DOWN"], "DOWN", params, state)
    assert len(signals) > 0
    for s in signals:
        assert s.setup_type == KIND
        assert s.stop_price < s.entry_price
        assert s.fill_date > s.entry_date


def test_replay_empty_state_returns_empty():
    universe = {
        "SPY": _df_constant_drift(5, 100.0, 0.0),  # insufficient history
        "X": _df_constant_drift(5, 100.0, 0.0),
    }
    params = _params()
    state = precompute(universe, params)
    assert state.rebalance_dates == []
    assert replay(universe["X"], "X", params, state) == []


def test_replay_no_signal_for_ticker_not_in_bottom_n():
    universe = {
        "SPY": _df_constant_drift(60, 100.0, 0.0),
        "UP": _df_constant_drift(60, 100.0, 0.005),
        "DOWN": _df_constant_drift(60, 100.0, -0.005),
    }
    params = _params()
    params["bottom_n"] = 1
    state = precompute(universe, params)
    up_signals = replay(universe["UP"], "UP", params, state)
    assert up_signals == []
