"""Tests for tools.upside_screen — pure scoring + ranking (no network)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.upside_screen import rank, score_ticker


def _mk(close: np.ndarray, vol: float = 2e6) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=len(close), freq="D")
    return pd.DataFrame(
        {
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": np.full(len(close), vol),
        },
        index=idx,
    )


N = 260
T = np.arange(N, dtype=float)


def test_smooth_uptrend_scores_high_and_qualifies():
    close = 100.0 * np.exp(0.002 * T)  # steady ~+66%/yr, no noise
    r = score_ticker("SMOOTH", _mk(close))
    assert r is not None
    assert r["qualifies"] is True
    assert r["above_trend_sma"] is True
    assert r["r2"] > 0.95          # very smooth
    assert r["score"] > 0          # positive momentum


def test_choppy_uptrend_scores_lower_than_smooth_same_drift():
    rng = np.random.default_rng(42)
    drift = 100.0 * np.exp(0.002 * T)
    smooth = score_ticker("SMOOTH", _mk(drift))
    choppy_px = drift * (1 + rng.normal(0, 0.06, N))   # same trend, added noise
    choppy = score_ticker("CHOPPY", _mk(choppy_px))
    assert smooth is not None and choppy is not None
    # The R^2 term penalises choppiness -> lower score for the same drift.
    assert choppy["r2"] < smooth["r2"]
    assert choppy["score"] < smooth["score"]


def test_downtrend_does_not_qualify():
    close = 200.0 * np.exp(-0.002 * T)
    r = score_ticker("DOWN", _mk(close))
    assert r is not None
    assert r["above_trend_sma"] is False
    assert r["qualifies"] is False
    assert r["score"] < 0


def test_illiquid_name_does_not_qualify():
    close = 100.0 * np.exp(0.002 * T)
    r = score_ticker("THIN", _mk(close, vol=100_000), adv_floor_m=1.0)
    assert r is not None
    assert r["liquid"] is False
    assert r["qualifies"] is False


def test_gap_flag_set_on_big_single_day_move():
    close = 100.0 * np.exp(0.001 * T)
    close[-30] *= 1.20  # +20% one-day jump inside the lookback window
    close[-29:] *= 1.20
    r = score_ticker("GAPPER", _mk(close))
    assert r is not None
    assert r["gap_flag"] is True
    assert r["max_gap_pct"] > 15.0


def test_rank_orders_by_score_and_drops_non_qualifiers():
    fast = score_ticker("FAST", _mk(100.0 * np.exp(0.003 * T)))
    slow = score_ticker("SLOW", _mk(100.0 * np.exp(0.0008 * T)))
    down = score_ticker("DOWN", _mk(200.0 * np.exp(-0.002 * T)))
    ranked = rank([slow, down, fast])
    tickers = [r["ticker"] for r in ranked]
    assert tickers == ["FAST", "SLOW"]   # DOWN dropped, FAST before SLOW
    assert "DOWN" not in tickers


def test_too_short_series_returns_none():
    close = 100.0 * np.exp(0.002 * np.arange(40, dtype=float))
    assert score_ticker("SHORT", _mk(close)) is None
