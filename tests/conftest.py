"""Shared fixtures for Phase 2 tool tests.

Tests use **synthetic** OHLCV data so they're deterministic, network-free,
and licence-clean. Real yfinance fetches are exercised only via the CLI
during manual verification.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _make_ohlcv(
    n: int,
    start_price: float,
    trend_pct_per_bar: float = 0.0,
    seed: int = 42,
    base_volume: int = 1_000_000,
    volatility_pct: float = 1.0,
) -> pd.DataFrame:
    """Generate synthetic OHLCV with controllable trend + noise.

    Each bar's Close = prev_close × (1 + trend + noise). High/Low expand
    around Close by ±half the daily volatility.
    """
    rng = np.random.default_rng(seed)
    closes = [start_price]
    for _ in range(n - 1):
        noise = rng.normal(0, volatility_pct / 100.0)
        closes.append(closes[-1] * (1 + trend_pct_per_bar / 100.0 + noise))
    closes_arr = np.array(closes)
    half_range = closes_arr * (volatility_pct / 200.0)
    highs = closes_arr + half_range
    lows = closes_arr - half_range
    opens = np.concatenate([[start_price], closes_arr[:-1]])
    volumes = rng.integers(
        int(base_volume * 0.7), int(base_volume * 1.3), size=n
    ).astype(int)
    idx = pd.date_range("2024-01-02", periods=n, freq="B")  # business days
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes_arr, "Volume": volumes},
        index=idx,
    )


@pytest.fixture
def uptrend_ohlcv() -> pd.DataFrame:
    """Strong uptrend, 400 bars (~1.5y). Stage 2 candidate. Trend >> noise."""
    return _make_ohlcv(
        n=400, start_price=100.0, trend_pct_per_bar=0.30, seed=1, volatility_pct=0.5
    )


@pytest.fixture
def downtrend_ohlcv() -> pd.DataFrame:
    """Sustained downtrend. Stage 4 candidate."""
    return _make_ohlcv(
        n=400, start_price=200.0, trend_pct_per_bar=-0.30, seed=2, volatility_pct=0.5
    )


@pytest.fixture
def flat_ohlcv() -> pd.DataFrame:
    """Range-bound. Stage 1 candidate. Low noise so no spurious trends."""
    return _make_ohlcv(
        n=400, start_price=100.0, trend_pct_per_bar=0.0, seed=3, volatility_pct=0.1
    )


def _vcp_synthetic() -> pd.DataFrame:
    """OHLCV hand-shaped to contain a VCP with breakout on last bar.

    Three progressive contractions (depths 18% → 11% → 4%) over ~60 bars,
    then a final bar at 1.05× pivot on 1.6× volume.
    """
    # 240 bars of pre-history (uptrend)
    pre = _make_ohlcv(n=240, start_price=80.0, trend_pct_per_bar=0.05, seed=10)

    # VCP shape: define the 60-bar window manually.
    # Anchor at 100 = end of pre-history.
    anchor = float(pre["Close"].iloc[-1])
    pattern_closes = []
    # Contraction 1: rise 22% then drop 18%  (peak=anchor*1.22, trough=peak*0.82)
    peak1 = anchor * 1.22
    trough1 = peak1 * 0.82
    rise1 = np.linspace(anchor, peak1, 12)
    drop1 = np.linspace(peak1, trough1, 8)[1:]
    # Contraction 2: rise to ~peak1*1.04 then drop 11%
    peak2 = peak1 * 1.04
    trough2 = peak2 * 0.89
    rise2 = np.linspace(trough1, peak2, 10)[1:]
    drop2 = np.linspace(peak2, trough2, 7)[1:]
    # Contraction 3: rise to ~peak2*1.02 then drop 4%
    peak3 = peak2 * 1.02
    trough3 = peak3 * 0.96
    rise3 = np.linspace(trough2, peak3, 9)[1:]
    drop3 = np.linspace(peak3, trough3, 6)[1:]
    # Breakout bar
    breakout = peak3 * 1.05
    rise_to_breakout = np.linspace(trough3, breakout, 9)[1:]
    pattern_closes = np.concatenate(
        [rise1, drop1, rise2, drop2, rise3, drop3, rise_to_breakout]
    )
    n_pat = len(pattern_closes)

    closes = np.concatenate([pre["Close"].to_numpy(), pattern_closes])
    n = len(closes)
    rng = np.random.default_rng(99)
    half_range = closes * 0.005
    highs = closes + half_range
    lows = closes - half_range
    opens = np.concatenate([[80.0], closes[:-1]])
    base_vol = rng.integers(900_000, 1_100_000, size=n).astype(int)
    # Spike volume on the breakout bar.
    base_vol[-1] = int(base_vol[-21:-1].mean() * 1.6)
    idx = pd.date_range("2024-01-02", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": base_vol},
        index=idx,
    )


@pytest.fixture
def vcp_ohlcv() -> pd.DataFrame:
    return _vcp_synthetic()
