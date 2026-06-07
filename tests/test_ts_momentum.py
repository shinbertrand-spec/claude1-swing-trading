"""Tests for the time-series-momentum (TSMOM) kind plugin — top-K rank variant.

Validates the 2026-06-07 fix: explicit cross-sectional top-K ranking replaces the
v1 no-rank behaviour (where the concurrent cap arbitrarily rejected ~98.5% of
signals). precompute now ranks positive-trailing-return names and keeps the top
K; replay emits signals only for top-K members.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._kinds.ts_momentum import (
    KIND,
    CrossSectionalState,
    precompute,
    replay,
)


def _df(drift: float, n: int = 120, start: float = 100.0) -> pd.DataFrame:
    closes = np.array([start * np.exp(drift * i) for i in range(n)])
    return pd.DataFrame(
        {"Open": closes, "High": closes * 1.005, "Low": closes * 0.995,
         "Close": closes, "Volume": np.full(n, 1_000_000, dtype=int)},
        index=pd.date_range("2022-01-03", periods=n, freq="B"),
    )


def _universe():
    return {
        "SPY": _df(0.0015),
        "A": _df(0.004),    # strongest uptrend
        "B": _df(0.001),    # mild uptrend
        "C": _df(0.0),      # flat  -> trailing return 0 -> excluded
        "D": _df(-0.003),   # downtrend -> excluded
    }


def _params(*, lookback=30, top_k=2):
    return {
        "benchmark": "SPY", "lookback_days": lookback, "rebalance_period_days": 21,
        "max_hold_days": 21, "top_k": top_k, "atr_period": 20, "atr_stop_multiple": 3.0,
    }


def test_kind_registry_has_strategy():
    assert KIND == "ts_momentum"
    assert KIND in KIND_REGISTRY


def test_precompute_returns_state_with_rebalance_dates():
    state = precompute(_universe(), _params())
    assert isinstance(state, CrossSectionalState)
    assert state.rebalance_dates                   # non-empty
    assert all(isinstance(s, set) for s in state.ranks_by_date.values())


def test_topk_keeps_only_strongest_positive_names():
    state = precompute(_universe(), _params(top_k=2))
    # Every rebalance date: top-2 positive names are A then B; C/D excluded.
    for d in state.rebalance_dates:
        assert state.ranks_by_date[d] == {"A", "B"}
        # A's trailing return > B's
        assert state.score_by_date[d]["A"] > state.score_by_date[d]["B"] > 0


def test_topk_one_keeps_single_strongest():
    state = precompute(_universe(), _params(top_k=1))
    for d in state.rebalance_dates:
        assert state.ranks_by_date[d] == {"A"}


def test_replay_emits_for_topk_member():
    uni = _universe()
    state = precompute(uni, _params(top_k=2))
    sigs = replay(uni["A"], "A", _params(top_k=2), state)
    assert len(sigs) >= 1
    for s in sigs:
        assert s.setup_type == KIND
        assert s.stop_price < s.entry_price
        assert s.fill_date > s.entry_date
        assert s.notes["trailing_return"] > 0


def test_replay_no_signal_for_non_topk_name():
    uni = _universe()
    state = precompute(uni, _params(top_k=2))
    assert replay(uni["C"], "C", _params(top_k=2), state) == []   # flat, excluded
    assert replay(uni["D"], "D", _params(top_k=2), state) == []   # down, excluded


def test_replay_excluded_when_capped_out():
    # With top_k=1 only A qualifies; B (positive but rank 2) gets no signal.
    uni = _universe()
    state = precompute(uni, _params(top_k=1))
    assert replay(uni["A"], "A", _params(top_k=1), state) != []
    assert replay(uni["B"], "B", _params(top_k=1), state) == []


def test_replay_skips_benchmark():
    uni = _universe()
    state = precompute(uni, _params())
    assert replay(uni["SPY"], "SPY", _params(), state) == []


def test_replay_none_state_returns_empty():
    uni = _universe()
    assert replay(uni["A"], "A", _params(), None) == []


def test_precompute_insufficient_history_empty_state():
    short = {"SPY": _df(0.0015, n=20), "A": _df(0.004, n=20)}
    state = precompute(short, _params(lookback=30))
    assert state.rebalance_dates == []
    assert replay(short["A"], "A", _params(lookback=30), state) == []
