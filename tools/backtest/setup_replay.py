"""Walk historical OHLCV day-by-day; fire Phase 2 setup detectors at each bar.

For each bar at index ``i`` in the historical series, slice ``df[:i+1]``
(everything up to and including that bar — no look-ahead) and run the
setup detector. If detected, emit a :class:`TradeSignal` with entry on
the **next** bar's open (standard backtest convention to avoid look-ahead
on the signal bar itself).

Phase 5.a ships SEPA-VCP replay only. Other setups follow the same
contract; add to :data:`SETUP_REPLAY_REGISTRY` as they're integrated.

CLI: not provided directly — use :mod:`tools.backtest.runner`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import pandas as pd

from ..vcp_detect import compute_from_ohlcv as vcp_compute
from ..trend_template import compute_from_ohlcv as trend_compute
from ..atr_compute import compute_from_ohlcv as atr_compute
from ..stop_sizer import compute as stop_compute


@dataclass
class TradeSignal:
    ticker: str
    setup_type: str
    setup_grade: str
    entry_date: date              # day signal was generated
    fill_date: date               # day the trade gets filled (next bar)
    entry_price: float            # next-bar open
    stop_price: float
    target_price: float | None
    max_hold_days: int
    atr_at_signal: float
    notes: dict = field(default_factory=dict)


def _detect_sepa_vcp_at_bar(
    df_slice: pd.DataFrame,
    min_history_bars: int = 260,
) -> tuple[bool, str | None, dict]:
    """Return (detected, grade, evidence) at the last bar of ``df_slice``.

    Uses :mod:`tools.vcp_detect` + :mod:`tools.trend_template` to require:

    * Trend template passes >= 6 (Stage 2)
    * VCP detected on the trailing 12 weeks
    * Breakout confirmed (above pivot + volume ratio >= 1.4)
    """
    if len(df_slice) < min_history_bars:
        return False, None, {"reason": f"insufficient history ({len(df_slice)} < {min_history_bars})"}

    try:
        tt = trend_compute(df_slice, include_rs=False)
    except ValueError:
        return False, None, {"reason": "trend_template error"}

    if tt.output["stage"] != 2 or tt.output["trend_template_passes"] < 6:
        return False, None, {
            "reason": "not stage 2",
            "trend_template_passes": tt.output["trend_template_passes"],
            "stage": tt.output["stage"],
        }

    try:
        vcp = vcp_compute(df_slice, weeks=12)
    except ValueError:
        return False, None, {"reason": "vcp_detect error"}

    if not vcp.output["detected"]:
        return False, None, {"reason": "vcp not detected", "contractions": vcp.output["contractions_count"]}

    if not vcp.output["breakout_confirmed"]:
        return False, None, {"reason": "vcp detected but breakout not confirmed today"}

    # Phase 5.a baseline grading: A if 3 contractions + sweet final, B otherwise.
    n_contractions = vcp.output["contractions_count"]
    final_pct = vcp.output["final_depth_pct"]
    if n_contractions >= 3 and final_pct is not None and final_pct <= 3.0:
        grade = "A"
    elif n_contractions >= 2 and final_pct is not None and final_pct <= 5.0:
        grade = "B"
    else:
        grade = "C"

    return True, grade, {
        "contractions": n_contractions,
        "final_depth_pct": final_pct,
        "pivot": vcp.output["pivot"],
        "volume_ratio": vcp.output["volume_ratio"],
    }


def replay_sepa_vcp(
    df: pd.DataFrame,
    ticker: str,
    start_index: int = 260,
    max_hold_days: int = 30,
    target_r_multiple: float = 2.0,
) -> list[TradeSignal]:
    """Walk ``df`` bar-by-bar, emit a TradeSignal each time SEPA-VCP fires.

    Args:
        df: OHLCV DataFrame indexed by date.
        ticker: ticker symbol (for the TradeSignal).
        start_index: skip the first N bars so detectors have history.
        max_hold_days: simulator exits after this many bars if no
            stop / target triggers first.
        target_r_multiple: target = entry + R-multiple × (entry - stop).
            Default 2.0 per CLAUDE.md minimum R:R rule.

    Returns:
        list of :class:`TradeSignal`. Empty if nothing fires.
    """
    signals: list[TradeSignal] = []
    if "Close" not in df.columns:
        raise ValueError("DataFrame missing 'Close' column")

    n = len(df)
    for i in range(start_index, n - 1):  # need i+1 for next-bar entry
        df_slice = df.iloc[: i + 1]
        detected, grade, evidence = _detect_sepa_vcp_at_bar(df_slice)
        if not detected:
            continue

        # Use the close as proxy "next open" if we don't have a real next bar
        # to avoid look-ahead; here we have it (i < n - 1), so use next Open.
        next_bar = df.iloc[i + 1]
        entry_price = float(next_bar["Open"])
        pivot = evidence["pivot"]

        # ATR for stop sizing.
        try:
            atr_entry = atr_compute(df_slice, period=14)
            atr_value = atr_entry.output["atr"]
        except ValueError:
            continue
        stop_entry = stop_compute(entry_price=entry_price, atr=atr_value, atr_multiple=2.0)
        stop_distance = stop_entry.output["stop_distance"]
        if stop_entry.output["skip_signal_atr_exceeds_cap"]:
            # ATR too wide for the setup — skip per swing-position-sizing.
            continue
        stop_price = stop_entry.output["stop_price"]
        target_price = entry_price + target_r_multiple * stop_distance

        signal_date = pd.Timestamp(df.index[i]).date()
        fill_date = pd.Timestamp(df.index[i + 1]).date()
        signals.append(
            TradeSignal(
                ticker=ticker,
                setup_type="SEPA-VCP",
                setup_grade=grade,
                entry_date=signal_date,
                fill_date=fill_date,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                max_hold_days=max_hold_days,
                atr_at_signal=atr_value,
                notes=evidence,
            )
        )
    return signals


# Registry maps setup_type → replay function. Phase 5.a ships SEPA-VCP;
# Phase 5.b registers EP + 3 secondaries below via side-effect imports.
SETUP_REPLAY_REGISTRY: dict[str, Callable[..., list[TradeSignal]]] = {
    "SEPA-VCP": replay_sepa_vcp,
}


# Side-effect imports: each module appends its replay function to the
# registry on import. Imported AFTER the registry is defined so the
# circular-import dance resolves cleanly.
from . import ep_replay as _ep_replay  # noqa: E402, F401
# Pullback-20SMA retired 2026-05-24 — 109-ticker rolling walk-forward sweep
# returned Sharpe -1.08 / DD -36% raw (structurally negative edge). The
# detector tools.pullback_detect is still available for discretionary use.
# from . import pullback_replay as _pullback_replay  # noqa: E402, F401
from . import rsi_div_replay as _rsi_div_replay  # noqa: E402, F401
from . import resistance_break_replay as _resistance_break_replay  # noqa: E402, F401
