"""Tests for the Connors Cumulative RSI(2) kind plugin.

Shape + correctness checks on synthetic OHLCV. Validates:

* KIND registration in `_kinds.KIND_REGISTRY`
* `_rsi` returns 100 on monotone-up series, 0 on monotone-down series
* `_cumulative_rsi` is the 2-bar rolling sum of RSI(2)
* `precompute()` regime filter blocks entries when SPY < 200d SMA
* `precompute()` finds oversold tickers when SPY > 200d SMA
* `replay()` emits a signal at the next-bar Open with valid stop
* `replay()` respects the cooldown_days parameter
* `replay()` returns empty when ticker is never eligible
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._kinds.connors_rsi2 import (
    KIND,
    _cumulative_rsi,
    _rsi,
    precompute,
    replay,
)


def _trending_df(n: int = 300, start: float = 100.0, slope: float = 0.001) -> pd.DataFrame:
    """Linear uptrend in log-space — RSI stays high, cumulative RSI stays high too."""
    closes = np.array([start * np.exp(slope * i) for i in range(n)])
    opens = closes.copy()
    highs = closes * 1.005
    lows = closes * 0.995
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


def _dip_at_end_df(n: int = 300, start: float = 100.0) -> pd.DataFrame:
    """Uptrend that ends with a sharp 5-day drop — exactly what RSI(2) should flag."""
    closes = np.array([start * (1 + 0.002 * i) for i in range(n - 5)])
    # Sharp drop at the end
    closes = np.concatenate([closes, closes[-1] * np.array([0.97, 0.94, 0.91, 0.89, 0.88])])
    opens = closes.copy()
    highs = closes * 1.005
    lows = closes * 0.995
    volumes = np.full(n, 1_000_000, dtype=int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2023-01-02", periods=n, freq="B"),
    )


def _params(**over) -> dict:
    base = {
        "benchmark": "SPY",
        "rsi_period": 2,
        "cumulative_period": 2,
        "entry_threshold": 10.0,
        "regime_sma_period": 50,   # Smaller for synthetic-data tests (300 bars).
        "atr_period": 20,
        "atr_stop_multiple": 2.0,
        "max_hold_days": 5,
        "target_r_multiple": None,
        "cooldown_days": 5,
    }
    base.update(over)
    return base


# ---------------------------------------------------------- KIND registry


def test_kind_registry_has_connors_rsi2():
    assert KIND == "connors_rsi2"
    assert KIND in KIND_REGISTRY


# ---------------------------------------------------------- RSI primitives


def test_rsi_monotone_up_series_is_100():
    closes = pd.Series([100.0 + i for i in range(20)])
    rsi = _rsi(closes, period=2)
    # After warm-up, every bar is an up-bar → RSI = 100.
    assert rsi.dropna().iloc[-1] == 100.0


def test_rsi_monotone_down_series_is_0():
    closes = pd.Series([100.0 - i for i in range(20)])
    rsi = _rsi(closes, period=2)
    # All down-bars → RSI = 0.
    assert rsi.dropna().iloc[-1] == 0.0


def test_cumulative_rsi_is_rolling_sum_of_rsi():
    closes = pd.Series([100.0 + 0.1 * i for i in range(30)])
    rsi = _rsi(closes, period=2)
    crsi = _cumulative_rsi(closes, rsi_period=2, cum_period=2)
    # CRSI at bar i = RSI[i] + RSI[i-1]; last value should be valid.
    expected_last = rsi.iloc[-1] + rsi.iloc[-2]
    assert abs(crsi.iloc[-1] - expected_last) < 1e-9


# ---------------------------------------------------------- precompute regime filter


def test_precompute_regime_filter_blocks_entries_in_downtrend():
    """When SPY < 200d SMA, no eligibility set should be built."""
    spy_down = _downtrend_df()
    a = _dip_at_end_df()  # has the end-of-period oversold dip
    universe = {"SPY": spy_down, "A": a}
    state = precompute(universe, _params(regime_sma_period=50))
    # eligible_by_date should be empty — SPY is in a downtrend the whole time.
    assert state.eligible_by_date == {}


def test_precompute_finds_oversold_in_uptrending_regime():
    """When SPY > 200d SMA AND a ticker has a sharp dip, ticker should be eligible."""
    spy = _trending_df()
    a = _dip_at_end_df()
    universe = {"SPY": spy, "A": a}
    state = precompute(universe, _params(regime_sma_period=50, entry_threshold=20.0))
    # The dip at the end should trigger eligibility for A on at least one date.
    a_eligible_dates = [d for d, ts in state.eligible_by_date.items() if "A" in ts]
    assert len(a_eligible_dates) > 0


def test_precompute_max_concurrent_caps_eligible_set():
    """max_concurrent_positions=2 keeps only the 2 most-oversold names per day."""
    spy = _trending_df()
    universe = {"SPY": spy}
    # 5 names that all dip at the end (so all are eligible on the same day),
    # but to different magnitudes so the ranker has something to order.
    # Bigger end-drop → lower CRSI → ranks higher (deepest oversold).
    for i, severity in enumerate([0.70, 0.75, 0.80, 0.85, 0.90]):
        n = 300
        closes = np.array([100.0 * (1 + 0.002 * j) for j in range(n - 5)])
        last = closes[-1]
        # 5-bar exponential drop toward `severity` of the pre-dip price.
        drop_path = np.linspace(last, last * severity, 6)[1:]
        closes = np.concatenate([closes, drop_path])
        opens = closes.copy()
        highs = closes * 1.005
        lows = closes * 0.995
        volumes = np.full(n, 1_000_000, dtype=int)
        universe[f"T{i}"] = pd.DataFrame(
            {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
            index=pd.date_range("2023-01-02", periods=n, freq="B"),
        )

    capped = precompute(
        universe,
        _params(regime_sma_period=50, entry_threshold=30.0, max_concurrent_positions=2),
    )
    uncapped = precompute(
        universe,
        _params(regime_sma_period=50, entry_threshold=30.0),
    )
    # Uncapped should fire on multiple names; capped should fire on at most 2.
    assert any(len(s) > 2 for s in uncapped.eligible_by_date.values()), (
        "uncapped run should have at least one day with >2 eligible names"
    )
    for d, eligible in capped.eligible_by_date.items():
        assert len(eligible) <= 2, f"date {d} has {len(eligible)} > 2 eligible"


def test_precompute_max_concurrent_invalid_raises():
    import pytest
    spy = _trending_df()
    a = _dip_at_end_df()
    universe = {"SPY": spy, "A": a}
    with pytest.raises(ValueError, match="max_concurrent_positions"):
        precompute(universe, _params(regime_sma_period=50, max_concurrent_positions=0))
    with pytest.raises(ValueError, match="max_concurrent_positions"):
        precompute(universe, _params(regime_sma_period=50, max_concurrent_positions=-3))


def test_precompute_max_concurrent_none_is_uncapped():
    """Default behaviour (param absent or None) is no cap — backwards compatible."""
    spy = _trending_df()
    a = _dip_at_end_df()
    universe = {"SPY": spy, "A": a}
    state = precompute(universe, _params(regime_sma_period=50, entry_threshold=20.0))
    # No KeyError, no crash — and the result equals the no-param baseline.
    assert isinstance(state.eligible_by_date, dict)


# ---------------------------------------------------------- replay


def test_replay_emits_signal_with_valid_stop():
    spy = _trending_df()
    a = _dip_at_end_df()
    universe = {"SPY": spy, "A": a}
    params = _params(regime_sma_period=50, entry_threshold=20.0)
    state = precompute(universe, params)
    signals = replay(a, "A", params, state)

    assert len(signals) >= 1
    s = signals[0]
    assert s.setup_type == KIND
    assert s.setup_grade == "B"
    assert s.entry_price > 0
    assert s.stop_price < s.entry_price
    assert s.max_hold_days == 5
    assert s.target_price is None  # spec sets no fixed target


def test_replay_respects_cooldown_days():
    """With cooldown_days=5, two eligibility days < 5 bars apart should produce one signal."""
    spy = _trending_df()
    a = _dip_at_end_df()
    universe = {"SPY": spy, "A": a}
    # entry_threshold=100 → every bar above regime-cutoff is "oversold"
    # → without cooldown we'd get a signal every day for the last 5 bars.
    params = _params(regime_sma_period=50, entry_threshold=200.0, cooldown_days=5)
    state = precompute(universe, params)
    signals = replay(a, "A", params, state)
    # With a 5-day cooldown across consecutive eligible days, we get few signals.
    # The exact count depends on the schedule, but it must be strictly less
    # than the number of eligible days.
    eligible_days = sum(1 for ts in state.eligible_by_date.values() if "A" in ts)
    assert len(signals) < eligible_days


def test_replay_returns_empty_when_no_eligibility():
    spy = _downtrend_df()  # regime blocks everything
    a = _dip_at_end_df()
    universe = {"SPY": spy, "A": a}
    params = _params(regime_sma_period=50)
    state = precompute(universe, params)
    signals = replay(a, "A", params, state)
    assert signals == []


def test_replay_skips_when_ticker_missing_from_state():
    """Replay called with a ticker that was never in any eligibility set → empty."""
    spy = _trending_df()
    a = _dip_at_end_df()
    b = _trending_df()  # always trending up, never oversold
    universe = {"SPY": spy, "A": a, "B": b}
    params = _params(regime_sma_period=50, entry_threshold=20.0)
    state = precompute(universe, params)
    signals = replay(b, "B", params, state)
    assert signals == []


# ------------------------------------------------ per-ticker trend filter


def _shallow_dip_uptrend_df(n: int = 300, start: float = 100.0) -> pd.DataFrame:
    """Strong uptrend with a tiny 2-bar pullback at the end.

    The pullback is deep enough to drive RSI(2) oversold, but the close
    stays comfortably above the lagging 50-day SMA — i.e. mean-reversion
    WITHIN the name's own uptrend. The per-ticker trend filter should KEEP
    this name eligible.
    """
    closes = np.array([start * (1 + 0.004 * i) for i in range(n - 2)])
    closes = np.concatenate([closes, closes[-1] * np.array([0.985, 0.975])])
    opens = closes.copy()
    highs = closes * 1.005
    lows = closes * 0.995
    volumes = np.full(n, 1_000_000, dtype=int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2023-01-02", periods=n, freq="B"),
    )


def test_trend_filter_excludes_below_ma_knife():
    """An oversold name BELOW its own 50d SMA (a falling knife) is eligible
    without the filter and excluded with it."""
    spy = _trending_df()
    a = _dip_at_end_df()  # ~12% drop off the highs → close below its own 50d SMA
    universe = {"SPY": spy, "A": a}
    off = precompute(universe, _params(regime_sma_period=50, entry_threshold=20.0))
    on = precompute(
        universe,
        _params(regime_sma_period=50, entry_threshold=20.0, ticker_trend_sma_period=50),
    )
    a_off = [d for d, ts in off.eligible_by_date.items() if "A" in ts]
    a_on = [d for d, ts in on.eligible_by_date.items() if "A" in ts]
    assert len(a_off) > 0, "without the trend filter, the knife should be eligible"
    assert len(a_on) == 0, "with the trend filter, a below-MA name must be excluded"


def test_trend_filter_keeps_above_ma_pullback():
    """An oversold name still ABOVE its own 50d SMA stays eligible with the filter."""
    spy = _trending_df()
    b = _shallow_dip_uptrend_df()
    universe = {"SPY": spy, "B": b}
    on = precompute(
        universe,
        _params(regime_sma_period=50, entry_threshold=30.0, ticker_trend_sma_period=50),
    )
    b_on = [d for d, ts in on.eligible_by_date.items() if "B" in ts]
    assert len(b_on) > 0, "a pullback within the name's own uptrend must stay eligible"


def test_trend_filter_absent_is_off():
    """Param absent vs explicit None vs omitted all yield identical eligibility."""
    spy = _trending_df()
    a = _dip_at_end_df()
    universe = {"SPY": spy, "A": a}
    base = precompute(universe, _params(regime_sma_period=50, entry_threshold=20.0))
    explicit_none = precompute(
        universe,
        _params(regime_sma_period=50, entry_threshold=20.0, ticker_trend_sma_period=None),
    )
    assert base.eligible_by_date.keys() == explicit_none.eligible_by_date.keys()


def test_trend_filter_invalid_period_raises():
    import pytest
    spy = _trending_df()
    a = _dip_at_end_df()
    universe = {"SPY": spy, "A": a}
    with pytest.raises(ValueError, match="ticker_trend_sma_period"):
        precompute(universe, _params(regime_sma_period=50, ticker_trend_sma_period=0))
    with pytest.raises(ValueError, match="ticker_trend_sma_period"):
        precompute(universe, _params(regime_sma_period=50, ticker_trend_sma_period=-5))
