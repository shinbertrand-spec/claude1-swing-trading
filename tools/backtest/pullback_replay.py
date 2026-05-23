"""Pullback-to-20-SMA secondary setup replay.

Per ``swing-setup-library.md`` Secondary 1. Walks historical OHLCV
bar-by-bar; at each bar checks whether ``tools.pullback_detect`` fires
on the trailing window. If so, emit a :class:`TradeSignal`.

Stop = below the reversal candle low (suggested_stop from the detector).
Target = entry + R × (entry - stop), R=2.0 default per CLAUDE.md.
Max hold defaults to 21 bars (~1 month) — secondary setups are
shorter-duration than primaries.
"""
from __future__ import annotations

import pandas as pd

from ..atr_compute import compute_from_ohlcv as atr_compute
from ..pullback_detect import compute_from_ohlcv as pullback_compute
from .setup_replay import TradeSignal, SETUP_REPLAY_REGISTRY

MIN_HISTORY_BARS = 60


def replay_pullback(
    df: pd.DataFrame,
    ticker: str,
    start_index: int = MIN_HISTORY_BARS,
    max_hold_days: int = 21,
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
            r = pullback_compute(df_slice)
        except ValueError:
            continue
        if not r.output["detected"]:
            continue

        # Entry = next bar's open.
        next_bar = df.iloc[i + 1]
        entry_price = float(next_bar["Open"])

        suggested_stop = r.output.get("suggested_stop")
        if suggested_stop is None:
            continue
        stop_price = float(suggested_stop)
        if stop_price >= entry_price:
            continue
        # 8% Minervini cap.
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
                setup_type="Pullback-20SMA",
                setup_grade="A",   # Secondary 1 baseline grade per swing-setup-library
                entry_date=pd.Timestamp(df.index[i]).date(),
                fill_date=pd.Timestamp(df.index[i + 1]).date(),
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                max_hold_days=max_hold_days,
                atr_at_signal=atr_value,
                notes={
                    "candle_type": r.output.get("candle_type"),
                    "distance_pct": r.output["stats"]["distance_pct"],
                    "volume_ratio": r.output["stats"]["volume_ratio_today_vs_20d"],
                },
            )
        )
    return signals


SETUP_REPLAY_REGISTRY["Pullback-20SMA"] = replay_pullback
