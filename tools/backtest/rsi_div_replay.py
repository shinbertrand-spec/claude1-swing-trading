"""RSI(14) divergence secondary setup replay.

Per ``swing-setup-library.md`` Secondary 2. Walks historical OHLCV; at
each bar checks whether ``tools.rsi_divergence`` fires (price LL + RSI
HL at support + volume confirm).
"""
from __future__ import annotations

import pandas as pd

from ..atr_compute import compute_from_ohlcv as atr_compute
from ..rsi_divergence import compute_from_ohlcv as rsi_div_compute
from .setup_replay import TradeSignal, SETUP_REPLAY_REGISTRY

MIN_HISTORY_BARS = 300   # rsi_divergence requires >= 200 + swing window


def replay_rsi_divergence(
    df: pd.DataFrame,
    ticker: str,
    start_index: int = MIN_HISTORY_BARS,
    max_hold_days: int = 14,    # mean-reversion setup; tight hold
    target_r_multiple: float = 2.0,
) -> list[TradeSignal]:
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")

    signals: list[TradeSignal] = []
    n = len(df)
    for i in range(start_index, n - 1):
        df_slice = df.iloc[: i + 1]
        try:
            r = rsi_div_compute(df_slice)
        except ValueError:
            continue
        if not r.output.get("detected", False):
            continue

        next_bar = df.iloc[i + 1]
        entry_price = float(next_bar["Open"])

        suggested_stop = r.output.get("suggested_stop")
        if suggested_stop is None:
            continue
        stop_price = float(suggested_stop)
        if stop_price >= entry_price:
            continue
        cap_stop = entry_price * 0.92
        stop_price = max(stop_price, cap_stop)
        if stop_price >= entry_price:
            continue

        try:
            atr_value = atr_compute(df_slice, period=14).output["atr"]
        except ValueError:
            continue
        target_price = entry_price + target_r_multiple * (entry_price - stop_price)

        signals.append(
            TradeSignal(
                ticker=ticker,
                setup_type="RSI-Divergence",
                setup_grade="B",  # secondary setup, lower confidence
                entry_date=pd.Timestamp(df.index[i]).date(),
                fill_date=pd.Timestamp(df.index[i + 1]).date(),
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                max_hold_days=max_hold_days,
                atr_at_signal=atr_value,
                notes={
                    "swing_lows": r.output.get("swing_lows"),
                    "support_proximity": r.output.get("support_proximity"),
                },
            )
        )
    return signals


SETUP_REPLAY_REGISTRY["RSI-Divergence"] = replay_rsi_divergence
