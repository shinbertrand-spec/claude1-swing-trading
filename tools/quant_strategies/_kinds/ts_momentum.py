"""Time-Series Momentum (TSMOM) per Moskowitz-Ooi-Pedersen 2012.

Per the paperswithbacktest catalog "Time Series Momentum Effect"
(in-sample Sharpe ~0.576). Per-ticker absolute-return-sign signal —
distinct from cross-sectional momentum (Clenow) which always holds
top-K, and from dual-MA crossover which fires on MA crosses.

Logic:

* Every ``rebalance_period_days`` bars, compute trailing
  ``lookback_days`` return.
* If positive → emit long signal at next bar's open.
* If non-positive → no signal (sit out).
* ATR-based fixed stop; max-hold = rebalance period.

Regime-defensive cousin to Clenow: when a ticker's own trailing return
turns negative, it does not enter that period. Clenow always holds
top-K regardless of regime.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ...backtest.setup_replay import TradeSignal


KIND = "ts_momentum"


def precompute(universe_dfs: dict[str, pd.DataFrame], params: dict) -> None:
    """No cross-sectional state needed — per-ticker strategy."""
    return None


def replay(
    df: pd.DataFrame,
    ticker: str,
    params: dict,
    state: None,
) -> list[TradeSignal]:
    """Emit signal at each rebalance date when trailing return is positive."""
    benchmark = params.get("benchmark")
    if ticker == benchmark:
        return []

    lookback_days = int(params["lookback_days"])
    rebalance_period_days = int(params.get("rebalance_period_days", 21))
    max_hold_days = int(params.get("max_hold_days", 21))
    atr_period = int(params.get("atr_period", 20))
    atr_stop_multiple = float(params.get("atr_stop_multiple", 3.0))

    if "Open" not in df.columns or "Close" not in df.columns:
        raise ValueError(f"{ticker}: df missing Open/Close columns")
    if len(df) < lookback_days + 2:
        return []

    closes = df["Close"]
    signals: list[TradeSignal] = []
    df_index = list(df.index)

    first_eligible = lookback_days
    last_signal_idx = -10**9
    for i in range(first_eligible, len(df_index) - 1):
        if (i - first_eligible) % rebalance_period_days != 0:
            continue
        if i - last_signal_idx < rebalance_period_days:
            continue

        c_now = float(closes.iloc[i])
        c_then = float(closes.iloc[i - lookback_days])
        if c_then <= 0 or pd.isna(c_then) or pd.isna(c_now):
            continue
        trailing_return = (c_now / c_then) - 1.0
        if trailing_return <= 0:
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

        signal_date = pd.Timestamp(df_index[i]).date()
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
                target_price=None,
                max_hold_days=max_hold_days,
                atr_at_signal=atr_value,
                notes={
                    "trailing_return": trailing_return,
                    "lookback_days": lookback_days,
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
