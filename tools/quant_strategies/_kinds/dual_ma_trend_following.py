"""Dual-MA trend-following strategy.

Per Faber 2007 / classical dual-MA crossover / paperswithbacktest catalog
"Trend-following Effect in Stocks" (in-sample Sharpe ~0.569 daily).

Per-ticker (no cross-sectional state). Logic:

* Detect bar-on-bar SMA(``short_period``) crossing ABOVE SMA(``long_period``).
* Enter long next bar at open.
* ATR-based fixed stop (the simulator's ``trail: ma_trail`` mode can be
  set to make the stop migrate to the long-MA — set in YAML).
* Max-hold day cap (multi-week, matching Clenow's horizon for fair
  head-to-head comparison).

The direct head-to-head with Clenow v1: same universe, same period, same
gate. Clenow's regression-score ranking is the sophisticated alternative
to this dual-MA baseline. If dual-MA wins, Clenow's complexity isn't
earning its keep. If Clenow wins, the sophistication is justified.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ...backtest.setup_replay import TradeSignal


KIND = "dual_ma_trend_following"


def precompute(universe_dfs: dict[str, pd.DataFrame], params: dict) -> None:
    """No cross-sectional state needed — per-ticker strategy."""
    return None


def replay(
    df: pd.DataFrame,
    ticker: str,
    params: dict,
    state: None,
) -> list[TradeSignal]:
    """Emit a signal on each SMA(short) bullish cross above SMA(long)."""
    benchmark = params.get("benchmark")
    if ticker == benchmark:
        return []

    short_period = int(params["short_period"])
    long_period = int(params["long_period"])
    atr_period = int(params.get("atr_period", 20))
    atr_stop_multiple = float(params.get("atr_stop_multiple", 3.0))
    max_hold_days = int(params.get("max_hold_days", 60))
    target_r_multiple = params.get("target_r_multiple")
    if target_r_multiple is not None:
        target_r_multiple = float(target_r_multiple)

    # Cooldown to avoid same-bar re-entries when a position is still
    # implicitly "open" via the simulator. v1 shim: minimum gap between
    # consecutive signals = max(short_period // 2, 5) bars.
    cooldown_bars = max(short_period // 2, 5)

    if short_period >= long_period:
        raise ValueError(
            f"{ticker}: short_period ({short_period}) must be < long_period ({long_period})"
        )
    if "Open" not in df.columns or "Close" not in df.columns:
        raise ValueError(f"{ticker}: df missing Open/Close columns")
    if len(df) < long_period + 2:
        return []

    closes = df["Close"]
    sma_short = closes.rolling(window=short_period, min_periods=short_period).mean()
    sma_long = closes.rolling(window=long_period, min_periods=long_period).mean()

    signals: list[TradeSignal] = []
    df_index = list(df.index)
    last_signal_idx = -10**9
    for i in range(long_period, len(df_index) - 1):
        d = df_index[i]
        d_prev = df_index[i - 1]
        s_now = sma_short.loc[d]
        l_now = sma_long.loc[d]
        s_prev = sma_short.loc[d_prev]
        l_prev = sma_long.loc[d_prev]
        if pd.isna(s_now) or pd.isna(l_now) or pd.isna(s_prev) or pd.isna(l_prev):
            continue
        # Bullish cross: was at-or-below, now above.
        if s_prev > l_prev:
            continue
        if not (s_now > l_now):
            continue
        if i - last_signal_idx < cooldown_bars:
            continue

        next_bar = df.iloc[i + 1]
        entry_price = float(next_bar["Open"])
        if entry_price <= 0 or pd.isna(entry_price):
            continue

        atr_value = _compute_atr(df.iloc[: i + 1], period=atr_period)
        if atr_value is None or atr_value <= 0:
            continue
        stop_distance = atr_stop_multiple * atr_value
        stop_price = entry_price - stop_distance
        if stop_price >= entry_price:
            continue
        target_price = (
            entry_price + target_r_multiple * stop_distance
            if target_r_multiple is not None
            else None
        )

        signal_date = pd.Timestamp(d).date()
        fill_date = pd.Timestamp(df_index[i + 1]).date()
        signals.append(
            TradeSignal(
                ticker=ticker,
                setup_type=KIND,
                setup_grade="B",
                entry_date=signal_date,
                fill_date=fill_date,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                max_hold_days=max_hold_days,
                atr_at_signal=atr_value,
                notes={
                    "sma_short_at_signal": float(s_now),
                    "sma_long_at_signal": float(l_now),
                },
            )
        )
        last_signal_idx = i
    return signals


def _compute_atr(df: pd.DataFrame, period: int) -> float | None:
    """Plain ATR(period) on the last bar. Returns None if not enough data."""
    if len(df) < period + 1:
        return None
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    close = df["Close"].to_numpy(dtype=float)
    prev_close = np.roll(close, 1)
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    tr[0] = high[0] - low[0]
    return float(pd.Series(tr).rolling(window=period, min_periods=period).mean().iloc[-1])
