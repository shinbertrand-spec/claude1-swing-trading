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


def test_precompute_with_bottom_pct_picks_proportional_count():
    """bottom_pct=0.20 on a 10-name universe → bottom 2 picks per rebalance."""
    universe = {"SPY": _df_constant_drift(60, 100.0, 0.0)}
    # 10 names with descending drift: T0 = strongest uptrend → T9 = strongest downtrend.
    for i in range(10):
        drift = 0.005 - i * 0.001  # +0.005, +0.004, ..., -0.004
        universe[f"T{i}"] = _df_constant_drift(60, 100.0, drift)

    params = _params()
    params.pop("bottom_n")
    params["bottom_pct"] = 0.20  # 20% of 10 = 2
    state = precompute(universe, params)
    assert len(state.rebalance_dates) > 0
    # Bottom-2 on a 10-name pool should be T8 + T9 (most negative drift).
    for d in state.rebalance_dates:
        picked = state.bottom_n_by_date[d]
        assert picked == {"T8", "T9"}, f"unexpected bottom-2: {picked}"


def test_precompute_bottom_pct_wins_when_both_set():
    """bottom_pct takes precedence over bottom_n during migration."""
    universe = {"SPY": _df_constant_drift(60, 100.0, 0.0)}
    for i in range(10):
        universe[f"T{i}"] = _df_constant_drift(60, 100.0, 0.005 - i * 0.001)
    params = _params()
    params["bottom_n"] = 5
    params["bottom_pct"] = 0.10  # 10% of 10 = 1 ticker
    state = precompute(universe, params)
    for d in state.rebalance_dates:
        picked = state.bottom_n_by_date[d]
        assert len(picked) == 1, f"bottom_pct should win → 1 pick; got {picked}"


def test_precompute_bottom_pct_rounds_down_minimum_one():
    """Very small bottom_pct still picks at least 1 ticker."""
    universe = {"SPY": _df_constant_drift(60, 100.0, 0.0)}
    for i in range(10):
        universe[f"T{i}"] = _df_constant_drift(60, 100.0, 0.005 - i * 0.001)
    params = _params()
    params.pop("bottom_n")
    params["bottom_pct"] = 0.01  # 1% of 10 = 0.1 → max(1, 0) = 1
    state = precompute(universe, params)
    for d in state.rebalance_dates:
        assert len(state.bottom_n_by_date[d]) == 1


def test_precompute_invalid_bottom_pct_raises():
    """Out-of-range bottom_pct is rejected."""
    import pytest
    universe = {"SPY": _df_constant_drift(60, 100.0, 0.0), "X": _df_constant_drift(60, 100.0, 0.0)}
    params = _params()
    params.pop("bottom_n")
    for bad in (0.0, -0.1, 1.5):
        params["bottom_pct"] = bad
        with pytest.raises(ValueError, match="bottom_pct"):
            precompute(universe, params)


def test_precompute_missing_both_sizing_params_raises():
    import pytest
    universe = {"SPY": _df_constant_drift(60, 100.0, 0.0), "X": _df_constant_drift(60, 100.0, 0.0)}
    params = _params()
    params.pop("bottom_n")
    with pytest.raises(ValueError, match="bottom_pct or bottom_n"):
        precompute(universe, params)


# ------------------------------------------------ per-ticker trend filter


def test_trend_filter_excludes_below_ma_loser():
    """The biggest loser is a name below its own SMA; the trend filter
    drops it from the bottom-N pool, leaving only the above-trend name."""
    universe = {
        "SPY": _df_constant_drift(260, 100.0, 0.0),
        "UP": _df_constant_drift(260, 100.0, 0.004),    # uptrend → above own 50d SMA
        "DOWN": _df_constant_drift(260, 100.0, -0.004),  # downtrend → below own SMA, biggest loser
    }
    params = _params()
    params["bottom_n"] = 1
    params["ticker_trend_sma_period"] = 50
    state = precompute(universe, params)
    assert len(state.rebalance_dates) > 0
    up_picked_count = 0
    for d in state.rebalance_dates:
        picked = state.bottom_n_by_date[d]
        # DOWN is the biggest loser but below its own SMA → never picked.
        assert "DOWN" not in picked, "a below-MA falling knife must be filtered out"
        # Early dates lack 50 bars of history → no name clears the gate (empty).
        # Once UP has enough history it is the only survivor → {UP}.
        assert picked in ({"UP"}, set()), f"unexpected pick: {picked}"
        if picked == {"UP"}:
            up_picked_count += 1
    assert up_picked_count > 0, "UP should survive the trend gate on later dates"


def test_trend_filter_off_still_picks_biggest_loser():
    """Without the filter (default), DOWN is the biggest loser and is picked —
    proving the filter is what changes the behaviour, not the universe."""
    universe = {
        "SPY": _df_constant_drift(260, 100.0, 0.0),
        "UP": _df_constant_drift(260, 100.0, 0.004),
        "DOWN": _df_constant_drift(260, 100.0, -0.004),
    }
    params = _params()
    params["bottom_n"] = 1
    off = precompute(universe, dict(params))
    none = precompute(universe, {**params, "ticker_trend_sma_period": None})
    for d in off.rebalance_dates:
        assert "DOWN" in off.bottom_n_by_date[d]
        assert off.bottom_n_by_date[d] == none.bottom_n_by_date[d]


def test_trend_filter_invalid_period_raises():
    import pytest
    universe = {"SPY": _df_constant_drift(60, 100.0, 0.0), "X": _df_constant_drift(60, 100.0, 0.0)}
    params = _params()
    params["ticker_trend_sma_period"] = 0
    with pytest.raises(ValueError, match="ticker_trend_sma_period"):
        precompute(universe, params)
    params["ticker_trend_sma_period"] = -10
    with pytest.raises(ValueError, match="ticker_trend_sma_period"):
        precompute(universe, params)
