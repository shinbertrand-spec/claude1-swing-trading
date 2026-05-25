"""Tests for the cross-sectional low-volatility kind plugin.

Validates:
* `KIND` registration
* `_realized_volatility` returns NaN on insufficient/degenerate data
* `_realized_volatility` is larger for noisier series
* `precompute()` ranks lowest-vol tickers in bottom-N
* `precompute()` regime filter detects benchmark above/below SMA (v2)
* `replay()` emits signals only when ticker in bottom-N AND regime ok (v2)
* Stop invariants
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._kinds.xs_low_volatility import (
    KIND,
    _realized_volatility,
    precompute,
    replay,
)


def _df_constant_drift_with_noise(
    n: int, start: float, drift: float, noise: float, seed: int = 0
) -> pd.DataFrame:
    """OHLCV with optional Gaussian noise overlaid on the drift."""
    rng = np.random.default_rng(seed)
    log_returns = drift + noise * rng.standard_normal(n)
    closes = start * np.exp(np.cumsum(log_returns))
    opens = closes.copy()
    highs = closes * 1.005
    lows = closes * 0.995
    volumes = np.full(n, 1_000_000, dtype=int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2023-01-02", periods=n, freq="B"),
    )


def _trending_spy(n: int = 300, slope: float = 0.001, seed: int = 0) -> pd.DataFrame:
    """SPY-like series that trends up enough to clear its 200d SMA."""
    return _df_constant_drift_with_noise(n, 100.0, slope, 0.005, seed=seed)


def _downtrend_spy(n: int = 300, slope: float = -0.001, seed: int = 0) -> pd.DataFrame:
    """SPY-like series that trends down so the regime filter blocks entries."""
    return _df_constant_drift_with_noise(n, 100.0, slope, 0.005, seed=seed)


def test_kind_registry_has_strategy():
    assert KIND == "xs_low_volatility"
    assert KIND in KIND_REGISTRY


def test_realized_volatility_insufficient_history():
    assert np.isnan(_realized_volatility(pd.Series([100.0, 101.0]), lookback=60))


def test_realized_volatility_constant_returns_nan():
    closes = pd.Series([100.0] * 80)
    assert np.isnan(_realized_volatility(closes, lookback=60))


def test_realized_volatility_higher_for_noisier_series():
    quiet = pd.Series(100.0 * np.exp(np.cumsum(0.01 * np.random.default_rng(0).standard_normal(120))))
    noisy = pd.Series(100.0 * np.exp(np.cumsum(0.05 * np.random.default_rng(0).standard_normal(120))))
    v_quiet = _realized_volatility(quiet, lookback=60)
    v_noisy = _realized_volatility(noisy, lookback=60)
    assert np.isfinite(v_quiet) and np.isfinite(v_noisy)
    assert v_noisy > v_quiet


def _params(bottom_n: int = 1, regime_filter_period: int = 200) -> dict:
    return {
        "benchmark": "SPY",
        "lookback_days": 60,
        "bottom_n": bottom_n,
        "rebalance_period_days": 21,
        "max_hold_days": 21,
        "atr_period": 20,
        "atr_stop_multiple": 2.5,
        "risk_per_trade": 0.01,
        "regime_filter_period": regime_filter_period,
    }


def test_precompute_picks_lowest_vol_in_bottom_n():
    # SPY trending up so the regime filter passes — but the bottom-N ranking
    # is what this test asserts. 300 bars to clear the 200d SMA warmup.
    universe = {
        "SPY": _trending_spy(300),
        "QUIET": _df_constant_drift_with_noise(300, 100.0, 0.0005, 0.005),
        "NOISY": _df_constant_drift_with_noise(300, 100.0, 0.0005, 0.040),
    }
    params = _params(bottom_n=1)
    state = precompute(universe, params)
    assert len(state.rebalance_dates) > 0
    for d in state.rebalance_dates:
        picked = state.bottom_n_by_date[d]
        # QUIET should be in bottom-1; NOISY should not.
        if d in universe["QUIET"].index and d in universe["NOISY"].index:
            assert "QUIET" in picked
            assert "NOISY" not in picked


def test_replay_emits_signal_for_low_vol_ticker():
    universe = {
        "SPY": _trending_spy(300),
        "QUIET": _df_constant_drift_with_noise(300, 100.0, 0.0005, 0.005),
    }
    params = _params(bottom_n=1)
    state = precompute(universe, params)
    signals = replay(universe["QUIET"], "QUIET", params, state)
    assert len(signals) > 0
    for s in signals:
        assert s.setup_type == KIND
        assert s.stop_price < s.entry_price


def test_replay_empty_state_returns_empty():
    universe = {
        "SPY": _df_constant_drift_with_noise(5, 100.0, 0.0, 0.005),
        "X": _df_constant_drift_with_noise(5, 100.0, 0.0, 0.005),
    }
    params = _params()
    state = precompute(universe, params)
    assert state.rebalance_dates == []
    assert replay(universe["X"], "X", params, state) == []


# ---------------------------------------------------------------------------
# v2 — SPY 200d MA regime filter tests (mirror tests/test_quant_strategies_clenow.py)
# ---------------------------------------------------------------------------


def test_precompute_regime_filter_detects_uptrend():
    spy_up = _trending_spy(300)
    universe = {"SPY": spy_up, "X": _trending_spy(300, slope=0.0005)}
    state = precompute(universe, _params())
    last_date = spy_up.index[-1]
    assert state.benchmark_regime_ok.get(last_date) is True


def test_precompute_regime_filter_detects_downtrend():
    spy_down = _downtrend_spy(300)
    universe = {"SPY": spy_down, "X": _df_constant_drift_with_noise(300, 100.0, 0.0, 0.005)}
    state = precompute(universe, _params())
    last_date = spy_down.index[-1]
    # Down-trending SPY → below SMA → regime not OK
    assert state.benchmark_regime_ok.get(last_date) is False


def test_replay_blocks_signals_when_regime_off():
    # SPY in downtrend → regime filter blocks all entries even though the
    # candidate is low-vol and would otherwise be in bottom-N.
    spy_down = _downtrend_spy(300)
    quiet = _df_constant_drift_with_noise(300, 100.0, 0.0005, 0.005)
    universe = {"SPY": spy_down, "QUIET": quiet}
    params = _params(bottom_n=1)
    state = precompute(universe, params)
    # Bottom-N picks QUIET (only candidate) but regime filter should kill signals.
    assert len(state.rebalance_dates) > 0
    assert any("QUIET" in picks for picks in state.bottom_n_by_date.values())
    signals = replay(quiet, "QUIET", params, state)
    assert signals == []


def test_replay_with_regime_filter_disabled_emits_signals_in_downtrend():
    # regime_filter_period=0 disables the gate → same behavior as v1.
    spy_down = _downtrend_spy(300)
    quiet = _df_constant_drift_with_noise(300, 100.0, 0.0005, 0.005)
    universe = {"SPY": spy_down, "QUIET": quiet}
    params = _params(bottom_n=1, regime_filter_period=0)
    state = precompute(universe, params)
    assert state.benchmark_regime_ok == {}
    signals = replay(quiet, "QUIET", params, state)
    assert len(signals) > 0  # filter inactive → signals emitted in downtrend
