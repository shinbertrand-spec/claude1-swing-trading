"""Tests for the Clenow Stocks-on-the-Move kind plugin.

Shape + correctness checks on synthetic OHLCV. Validates:

* `KIND` registration in `_kinds.KIND_REGISTRY`
* `precompute()` produces ranked top-K per rebalance date
* `precompute()` regime filter detects benchmark above/below SMA
* `replay()` only emits signals when (in top-K) AND (regime ok) AND (next bar exists)
* `replay()` returns empty when state has no rebalance dates
* `_annualised_log_slope_r2` returns -inf on insufficient data, finite on enough
* Stop and target prices satisfy invariants (stop < entry, target > entry when set)
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._kinds.clenow_momentum import (
    KIND,
    _annualised_log_slope_r2,
    precompute,
    replay,
)


def _trending_df(n: int = 300, start: float = 100.0, slope: float = 0.001) -> pd.DataFrame:
    """Linear uptrend in log-space — generates a high regression score."""
    closes = np.array([start * np.exp(slope * i) for i in range(n)])
    opens = closes.copy()
    highs = closes * 1.005
    lows = closes * 0.995
    volumes = np.full(n, 1_000_000, dtype=int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2023-01-02", periods=n, freq="B"),
    )


def _flat_df(n: int = 300, level: float = 100.0) -> pd.DataFrame:
    closes = np.full(n, level)
    opens = closes.copy()
    highs = closes + 0.5
    lows = closes - 0.5
    volumes = np.full(n, 1_000_000, dtype=int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2023-01-02", periods=n, freq="B"),
    )


def _downtrend_df(n: int = 300, start: float = 100.0, slope: float = -0.001) -> pd.DataFrame:
    closes = np.array([start * np.exp(slope * i) for i in range(n)])
    opens = closes.copy()
    highs = closes * 1.005
    lows = closes * 0.995
    volumes = np.full(n, 1_000_000, dtype=int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2023-01-02", periods=n, freq="B"),
    )


def test_kind_registry_has_clenow():
    assert KIND == "clenow_momentum"
    assert KIND in KIND_REGISTRY


def test_annualised_log_slope_r2_insufficient_history():
    closes = pd.Series([100.0, 101.0, 102.0])
    score = _annualised_log_slope_r2(closes, lookback=90)
    assert score == float("-inf")


def test_annualised_log_slope_r2_uptrend_positive_score():
    closes = pd.Series([100.0 * np.exp(0.001 * i) for i in range(120)])
    score = _annualised_log_slope_r2(closes, lookback=90)
    assert score > 0.0
    assert np.isfinite(score)


def test_annualised_log_slope_r2_flat_score_near_zero():
    closes = pd.Series([100.0] * 120)
    score = _annualised_log_slope_r2(closes, lookback=90)
    # flat = zero slope; score = 0
    assert abs(score) < 1e-6


def test_annualised_log_slope_r2_downtrend_negative_score():
    closes = pd.Series([100.0 * np.exp(-0.001 * i) for i in range(120)])
    score = _annualised_log_slope_r2(closes, lookback=90)
    assert score < 0.0


def _params(top_k: int = 2) -> dict:
    return {
        "benchmark": "SPY",
        "regime_filter_period": 200,
        "momentum_lookback_days": 90,
        "rebalance_period_days": 5,
        "top_k": top_k,
        "atr_period": 20,
        "atr_stop_multiple": 3.0,
        "max_hold_days": 5,
        "target_r_multiple": None,
    }


def test_precompute_returns_state_with_rebalance_dates():
    spy = _trending_df()
    ticker_a = _trending_df(slope=0.002)  # strongest
    ticker_b = _trending_df(slope=0.001)  # next
    ticker_c = _flat_df()                  # weakest
    universe = {"SPY": spy, "A": ticker_a, "B": ticker_b, "C": ticker_c}

    state = precompute(universe, _params(top_k=2))
    assert len(state.rebalance_dates) > 0
    # Top-2 across rebal dates should consistently include the strongest names.
    for d in state.rebalance_dates:
        top_set = state.ranks_by_date[d]
        assert len(top_set) <= 2
        # 'A' (strongest) should be in top-2 once enough history exists.
        # (Skip the very-first rebal where R^2 may be unstable.)
    last_rebal = state.rebalance_dates[-1]
    assert "A" in state.ranks_by_date[last_rebal]


def test_precompute_regime_filter_detects_uptrend():
    spy_up = _trending_df()
    universe = {"SPY": spy_up, "X": _trending_df()}
    state = precompute(universe, _params())
    # By the end of the uptrend, SPY should be > 200d SMA → regime OK
    last_date = spy_up.index[-1]
    assert state.benchmark_regime_ok.get(last_date) is True


def test_precompute_regime_filter_detects_downtrend():
    spy_down = _downtrend_df()
    universe = {"SPY": spy_down, "X": _flat_df()}
    state = precompute(universe, _params())
    last_date = spy_down.index[-1]
    # Down-trending SPY → below SMA → regime not OK
    assert state.benchmark_regime_ok.get(last_date) is False


def test_replay_emits_signals_when_in_top_k_and_regime_ok():
    spy = _trending_df()
    a = _trending_df(slope=0.002)
    b = _flat_df()
    universe = {"SPY": spy, "A": a, "B": b}
    state = precompute(universe, _params(top_k=1))
    signals = replay(a, "A", _params(top_k=1), state)
    # Should have at least one signal (A is the only candidate, strong uptrend)
    assert len(signals) > 0
    s = signals[0]
    assert s.setup_type == KIND
    assert s.stop_price < s.entry_price
    assert s.entry_price > 0
    assert s.target_price is None  # target_r_multiple = None


def test_replay_emits_no_signals_when_regime_off():
    spy = _downtrend_df()  # regime off
    a = _trending_df(slope=0.002)
    universe = {"SPY": spy, "A": a}
    state = precompute(universe, _params())
    signals = replay(a, "A", _params(), state)
    # Regime filter blocks all entries
    assert signals == []


def test_replay_returns_empty_when_no_rebalance_dates():
    """Empty state.rebalance_dates → no signals."""
    short_spy = _trending_df(n=50)
    short_a = _trending_df(n=50)
    universe = {"SPY": short_spy, "A": short_a}
    # 50 bars is less than max(lookback=90, regime=200) → no rebalance dates
    state = precompute(universe, _params())
    assert state.rebalance_dates == []
    signals = replay(short_a, "A", _params(), state)
    assert signals == []


def test_precompute_raises_when_benchmark_missing():
    a = _trending_df()
    universe = {"A": a}  # no SPY
    with pytest.raises(ValueError, match="benchmark"):
        precompute(universe, _params())


def test_replay_target_price_set_when_target_r_multiple_given():
    spy = _trending_df()
    a = _trending_df(slope=0.002)
    universe = {"SPY": spy, "A": a}
    state = precompute(universe, _params(top_k=1))
    params = _params(top_k=1)
    params["target_r_multiple"] = 3.0
    signals = replay(a, "A", params, state)
    assert len(signals) > 0
    s = signals[0]
    assert s.target_price is not None
    assert s.target_price > s.entry_price
