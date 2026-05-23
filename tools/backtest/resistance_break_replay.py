"""Resistance-breakout (non-VCP) secondary setup replay.

Per ``swing-setup-library.md`` Secondary 3. Walks historical OHLCV; at
each bar checks whether ``tools.resistance_break`` fires (clean horizontal
resistance violated decisively with volume ≥ 1.5× 20d avg).
"""
from __future__ import annotations

import pandas as pd

from ..atr_compute import compute_from_ohlcv as atr_compute
from ..resistance_break import compute_from_ohlcv as resistance_compute
from ..stop_sizer import compute as stop_compute
from .setup_replay import TradeSignal, SETUP_REPLAY_REGISTRY

MIN_HISTORY_BARS = 120   # resistance_break needs 90 + swing window


def replay_resistance_break(
    df: pd.DataFrame,
    ticker: str,
    start_index: int = MIN_HISTORY_BARS,
    max_hold_days: int = 30,
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
            r = resistance_compute(df_slice)
        except ValueError:
            continue
        if not r.output.get("detected", False):
            continue

        next_bar = df.iloc[i + 1]
        entry_price = float(next_bar["Open"])

        try:
            atr_value = atr_compute(df_slice, period=14).output["atr"]
        except ValueError:
            continue
        stop_entry = stop_compute(entry_price=entry_price, atr=atr_value, atr_multiple=2.0)
        if stop_entry.output["skip_signal_atr_exceeds_cap"]:
            continue
        stop_price = stop_entry.output["stop_price"]
        target_price = entry_price + target_r_multiple * (entry_price - stop_price)

        signals.append(
            TradeSignal(
                ticker=ticker,
                setup_type="Resistance-Breakout",
                setup_grade="B",  # Secondary 3 baseline grade
                entry_date=pd.Timestamp(df.index[i]).date(),
                fill_date=pd.Timestamp(df.index[i + 1]).date(),
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                max_hold_days=max_hold_days,
                atr_at_signal=atr_value,
                notes={
                    "resistance_level": r.output["stats"]["resistance_level"],
                    "touches_in_cluster": r.output["stats"]["touches_in_cluster"],
                    "volume_ratio": r.output["stats"]["volume_ratio"],
                },
            )
        )
    return signals


SETUP_REPLAY_REGISTRY["Resistance-Breakout"] = replay_resistance_break
