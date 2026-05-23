"""Base-stage detector (per ``swing-sell-discipline.md`` Trigger #3).

# v1-preliminary: revisit after Minervini book v2 ingestion
# Phase 2 baseline heuristic — refine after walk-forward calibration

O'Neil / Minervini base-count theory: each multi-week consolidation after
a Stage 2 advance is a "base." 1st-stage bases break out with highest
sustain; 4th/5th-stage base breakouts statistically fail.

This is a hard problem in the general case. Phase 2 baseline approach:

* Find swing highs across the lookback period (default 1y).
* Count distinct multi-week (≥3w) consolidations between successive highs.
* Cap at 5; report ``new_high_today`` flag (today's close > prior 252-day
  high) as the late-stage exit trigger.

Late-stage decision matrix (per swing-sell-discipline):

* 1st-2nd stage: hold, normal trail
* 3rd stage: sell 1/3, tighter trail
* 4th-5th stage: **sell 50-100% on new high; do not chase**

CLI::

    uv run python -m tools.base_stage_detect NVDA
"""
from __future__ import annotations

import argparse

import pandas as pd

from .cli import emit
from .contract import TraceEntry
from .data import fetch_ohlcv

TOOL = "tools/base_stage_detect.py"

# Phase 2 baseline; refine in Phase 5 walk-forward.
SWING_WINDOW = 10              # centred-window for swing-high detection
MIN_BASE_BARS = 15             # ~3 weeks
PRIOR_HIGH_LOOKBACK = 252      # 1 year of trading days for "new high"


def _find_swing_highs(close: pd.Series, window: int = SWING_WINDOW) -> list[int]:
    """Return indices of bars where close > both neighbours (centred window)."""
    peaks: list[int] = []
    values = close.to_numpy()
    for i in range(window, len(values) - window):
        left = values[i - window : i]
        right = values[i + 1 : i + 1 + window]
        if values[i] > left.max() and values[i] > right.max():
            peaks.append(i)
    return peaks


def compute_from_ohlcv(df: pd.DataFrame) -> TraceEntry:
    """Estimate base stage on the most recent bar.

    Returns base_stage 1-5 (capped) plus ``new_high_today`` boolean for
    the late-stage exit trigger.
    """
    if "Close" not in df.columns:
        raise ValueError("DataFrame missing 'Close' column")
    if len(df) < PRIOR_HIGH_LOOKBACK + SWING_WINDOW:
        raise ValueError(
            f"need at least {PRIOR_HIGH_LOOKBACK + SWING_WINDOW} bars; got {len(df)}"
        )

    close = df["Close"].astype(float)

    # Restrict the base-count window to the last PRIOR_HIGH_LOOKBACK bars
    # (avoid counting bases from years ago when the trend is fresh).
    window_close = close.tail(PRIOR_HIGH_LOOKBACK).reset_index(drop=True)
    peaks = _find_swing_highs(window_close)

    # Count distinct consolidations: pairs of consecutive peaks separated
    # by at least MIN_BASE_BARS bars where the trough between them is
    # measurably below both peaks (i.e. a real pullback).
    base_count = 0
    for i in range(1, len(peaks)):
        gap = peaks[i] - peaks[i - 1]
        if gap < MIN_BASE_BARS:
            continue
        between = window_close.iloc[peaks[i - 1] : peaks[i] + 1]
        if len(between) < 2:
            continue
        trough = float(between.min())
        if trough < min(float(window_close.iloc[peaks[i - 1]]), float(window_close.iloc[peaks[i]])) * 0.95:
            base_count += 1

    # The current breakout (if any) is the (base_count+1)th base.
    base_stage = min(5, base_count + 1)

    # New-high check: today > prior 252-day high (excluding today).
    prior_high = float(close.iloc[-(PRIOR_HIGH_LOOKBACK + 1) : -1].max()) if len(close) > PRIOR_HIGH_LOOKBACK else float(close.iloc[:-1].max())
    today_close = float(close.iloc[-1])
    new_high_today = today_close > prior_high

    late_stage = base_stage >= 4
    late_stage_new_high = late_stage and new_high_today

    return TraceEntry(
        tool=TOOL,
        inputs={
            "rows": len(df),
            "last_close_date": str(df.index[-1]),
            "lookback_bars": PRIOR_HIGH_LOOKBACK,
            "swing_window": SWING_WINDOW,
            "v1_preliminary": True,
        },
        output={
            "base_stage": base_stage,
            "base_count_completed": base_count,
            "new_high_today": new_high_today,
            "late_stage_new_high_exit_signal": late_stage_new_high,
            "stats": {
                "today_close": today_close,
                "prior_252d_high": prior_high,
                "peaks_found": len(peaks),
            },
            "v1_preliminary_flag": True,
            "phase2_baseline_note": "Heuristic base counter; refine after Phase 5 walk-forward.",
        },
    )


def compute_from_ticker(ticker: str) -> TraceEntry:
    fetch = fetch_ohlcv(ticker, period="2y")
    entry = compute_from_ohlcv(fetch.df)
    entry.inputs = {
        **entry.inputs,
        "ticker": ticker,
        "source": fetch.source,
        "data_fetched_at": fetch.fetched_at,
    }
    return entry


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.base_stage_detect",
        description="Estimate base stage (1-5) + new-high flag. v1-preliminary.",
    )
    p.add_argument("ticker")
    args = p.parse_args()
    emit(compute_from_ticker(args.ticker))


if __name__ == "__main__":
    main()
