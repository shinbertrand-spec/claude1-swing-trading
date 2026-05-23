"""Climax-top pattern detector (per ``swing-sell-discipline.md`` Trigger #1).

# v1-preliminary: revisit after Minervini book v2 ingestion

Counts how many of 6 parabolic blow-off patterns are firing on the most
recent bar:

1. ``sharp_advance`` — 25-50%+ gain over the last 1-3 weeks (5-15 bars)
2. ``8_of_10_up_days`` — 8 of the last 10 closes higher than the previous
3. ``highest_ever_volume`` — today's volume is the highest in the available history
4. ``accelerated_advance`` — last 6-10 bars have an accelerating slope with ≤2 down days
5. ``largest_up_day`` — today's up-day point gain is the largest since the move began
6. ``widest_daily_spread`` — today's H-L spread is the widest of the move

The decision matrix (``sell_decision.py``) maps the count to action:

* 0-1 patterns: continue holding
* 2 patterns: sell 50%, tighten trail
* 3+ patterns: sell 75-100% into strength

CLI::

    uv run python -m tools.climax_top_detect NVDA --move-start 2026-04-15
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

import numpy as np
import pandas as pd

from .cli import emit
from .contract import TraceEntry
from .data import fetch_ohlcv

TOOL = "tools/climax_top_detect.py"

# Phase 2 baseline thresholds; refine after walk-forward calibration + Minervini book v2.
SHARP_ADVANCE_WINDOW = 10           # ~2 weeks
SHARP_ADVANCE_THRESHOLD = 0.25
UP_DAYS_WINDOW = 10
UP_DAYS_THRESHOLD = 8
ACCEL_WINDOW = 8
ACCEL_DOWN_DAYS_MAX = 2


def _to_date(d: str | date | datetime | None) -> date | None:
    if d is None:
        return None
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    return datetime.fromisoformat(str(d)).date()


def compute_from_ohlcv(
    df: pd.DataFrame,
    move_start: str | date | None = None,
) -> TraceEntry:
    """Detect which climax-top patterns are firing on the most recent bar.

    Args:
        df: OHLCV DataFrame indexed by date. Needs Close, Volume, High, Low.
        move_start: optional ISO date of the move's origin. Patterns
            4/5/6 measure "since the move began" — if not supplied,
            defaults to 30 bars ago.
    """
    required = {"Close", "Volume", "High", "Low"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")
    if len(df) < UP_DAYS_WINDOW + 2:
        raise ValueError(f"need at least {UP_DAYS_WINDOW + 2} bars; got {len(df)}")

    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)

    move_d = _to_date(move_start)
    if move_d is not None:
        idx_dates = [pd.Timestamp(ts).date() for ts in df.index]
        try:
            move_pos = idx_dates.index(move_d)
        except ValueError:
            move_pos = max(0, len(df) - 30)
    else:
        move_pos = max(0, len(df) - 30)

    # Pattern 1 — sharp advance over the last ~2 weeks.
    sa_window = min(SHARP_ADVANCE_WINDOW, len(close) - 1)
    sa_pct = (float(close.iloc[-1]) / float(close.iloc[-(sa_window + 1)])) - 1.0
    sharp_advance = sa_pct >= SHARP_ADVANCE_THRESHOLD

    # Pattern 2 — 8 of last 10 days up.
    last_10 = close.iloc[-(UP_DAYS_WINDOW + 1):]
    up_days = int((last_10.diff().iloc[1:] > 0).sum())
    up_days_pattern = up_days >= UP_DAYS_THRESHOLD

    # Pattern 3 — today's volume is the highest in the available history.
    today_vol = float(volume.iloc[-1])
    highest_vol = today_vol >= float(volume.max())

    # Pattern 4 — accelerated advance: in last ACCEL_WINDOW bars, slope is
    # increasing AND down days <= ACCEL_DOWN_DAYS_MAX.
    last_accel = close.iloc[-(ACCEL_WINDOW + 1):]
    diffs = last_accel.diff().iloc[1:].to_numpy()
    down_days_in_accel = int(np.sum(diffs < 0))
    # Acceleration: slope in second half > slope in first half.
    if len(diffs) >= 4:
        first_half = float(np.mean(diffs[: len(diffs) // 2]))
        second_half = float(np.mean(diffs[len(diffs) // 2 :]))
        accelerated = (
            second_half > first_half
            and down_days_in_accel <= ACCEL_DOWN_DAYS_MAX
        )
    else:
        accelerated = False

    # Pattern 5 — today's up-day move is the largest since move began.
    move_close = close.iloc[move_pos:]
    move_diffs = move_close.diff().iloc[1:]
    up_diffs = move_diffs[move_diffs > 0]
    today_diff = float(close.iloc[-1] - close.iloc[-2]) if len(close) >= 2 else 0.0
    largest_up_day = today_diff > 0 and today_diff >= (
        float(up_diffs.max()) if len(up_diffs) > 0 else float("inf")
    )

    # Pattern 6 — today's H-L spread is the widest of the move.
    move_high = high.iloc[move_pos:]
    move_low = low.iloc[move_pos:]
    move_spreads = move_high - move_low
    today_spread = float(high.iloc[-1] - low.iloc[-1])
    widest_spread = today_spread >= float(move_spreads.max())

    patterns = {
        "sharp_advance": sharp_advance,
        "8_of_10_up_days": up_days_pattern,
        "highest_ever_volume": highest_vol,
        "accelerated_advance": accelerated,
        "largest_up_day": largest_up_day,
        "widest_daily_spread": widest_spread,
    }
    firing_count = sum(1 for v in patterns.values() if v)

    return TraceEntry(
        tool=TOOL,
        inputs={
            "move_start": move_d.isoformat() if move_d else None,
            "move_pos_index": move_pos,
            "rows": len(df),
            "last_close_date": str(df.index[-1]),
            "v1_preliminary": True,
        },
        output={
            "patterns_firing": firing_count,
            "patterns": patterns,
            "stats": {
                "sharp_advance_pct": sa_pct,
                "up_days_in_last_10": up_days,
                "today_volume": today_vol,
                "max_volume": float(volume.max()),
                "down_days_in_accel_window": down_days_in_accel,
                "today_up_diff": today_diff,
                "today_spread": today_spread,
            },
            "v1_preliminary_flag": True,
        },
    )


def compute_from_ticker(ticker: str, move_start: str | date | None = None) -> TraceEntry:
    fetch = fetch_ohlcv(ticker, period="6mo")
    entry = compute_from_ohlcv(fetch.df, move_start=move_start)
    entry.inputs = {
        **entry.inputs,
        "ticker": ticker,
        "source": fetch.source,
        "data_fetched_at": fetch.fetched_at,
    }
    return entry


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.climax_top_detect",
        description="Count 6 climax-top patterns firing. v1-preliminary.",
    )
    p.add_argument("ticker")
    p.add_argument("--move-start", default=None)
    args = p.parse_args()
    emit(compute_from_ticker(args.ticker, move_start=args.move_start))


if __name__ == "__main__":
    main()
