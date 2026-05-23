"""Day 7 milestone check (per ``swing-earnings-pivot.md`` + ``swing-momentum-execution.md``).

A position has "survived Day 7" iff during the first 7 trading days post-entry:

* Price did NOT break the entry-day low (close OR intraday, configurable)
* Price did NOT close below the 10-day SMA

This is the trigger for ADD-ON #2 (Stage-3) per the pyramiding workflow,
restricted to Super Swan / Golden EP grades. It also marks the inflection
beyond which the EP statistically transitions to multi-month hold mode.

CLI::

    uv run python -m tools.day7_milestone_check SMCI --entry-date 2026-05-17 \\
        --entry-low 405.20
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

import pandas as pd

from .cli import emit
from .contract import TraceEntry
from .data import fetch_ohlcv

TOOL = "tools/day7_milestone_check.py"
MILESTONE_DAYS = 7
TRAIL_MA_PERIOD = 10


def _to_date(d: str | date | datetime) -> date:
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    return datetime.fromisoformat(str(d)).date()


def compute_from_ohlcv(
    df: pd.DataFrame,
    entry_date: str | date,
    entry_low: float | None = None,
    intraday_low_check: bool = True,
    milestone_days: int = MILESTONE_DAYS,
    trail_ma_period: int = TRAIL_MA_PERIOD,
) -> TraceEntry:
    """Evaluate the Day 7 milestone over a window of post-entry bars.

    Args:
        df: OHLCV DataFrame indexed by date, including bars BEFORE entry
            (for the 10-MA seed) and the first ``milestone_days`` bars
            after entry.
        entry_date: ISO-date string or date. The bar at this date is the
            entry bar; subsequent bars are evaluated.
        entry_low: optional explicit entry-bar low. If ``None``, taken
            from the ``Low`` column at ``entry_date``.
        intraday_low_check: if True, any bar with Low < entry_low fails
            the check. If False, only Close < entry_low fails (a looser
            "closed below entry low" interpretation).
        milestone_days: how many trading days post-entry to evaluate.
            Default 7 per the operational notes.
        trail_ma_period: SMA period for the trail trigger. Default 10.

    Returns:
        TraceEntry with output: ``survives_day7`` (bool),
        ``broke_entry_low`` (bool + which day), ``closed_below_10ma``
        (bool + which day), ``trading_days_since_entry``,
        ``entry_low_used``, ``milestone_window_dates``.
    """
    required = {"Open", "High", "Low", "Close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")
    entry_d = _to_date(entry_date)

    # Locate entry bar.
    idx_dates = [pd.Timestamp(ts).date() for ts in df.index]
    if entry_d not in idx_dates:
        raise ValueError(
            f"entry_date {entry_d} not in DataFrame index "
            f"(span: {idx_dates[0]} → {idx_dates[-1]})"
        )
    entry_pos = idx_dates.index(entry_d)
    if entry_pos < trail_ma_period - 1:
        raise ValueError(
            f"need at least {trail_ma_period} bars before entry for {trail_ma_period}-MA; "
            f"entry is at index {entry_pos}"
        )

    entry_bar = df.iloc[entry_pos]
    entry_low_used = float(entry_low) if entry_low is not None else float(entry_bar["Low"])

    # Days available for evaluation (capped at milestone_days).
    available_bars = len(df) - entry_pos - 1
    eval_n = min(milestone_days, available_bars)

    broke_entry_low = False
    broke_low_on_day: int | None = None
    closed_below_10ma = False
    closed_below_on_day: int | None = None
    window_dates: list[str] = []

    close = df["Close"].astype(float)
    low = df["Low"].astype(float)

    for offset in range(1, eval_n + 1):
        i = entry_pos + offset
        window_dates.append(str(idx_dates[i]))
        # 10-day SMA at bar i (uses bars i-(period-1)..i inclusive).
        ma_slice = close.iloc[i - trail_ma_period + 1 : i + 1]
        ma_value = float(ma_slice.mean())

        if intraday_low_check:
            if float(low.iloc[i]) < entry_low_used and not broke_entry_low:
                broke_entry_low = True
                broke_low_on_day = offset
        else:
            if float(close.iloc[i]) < entry_low_used and not broke_entry_low:
                broke_entry_low = True
                broke_low_on_day = offset

        if float(close.iloc[i]) < ma_value and not closed_below_10ma:
            closed_below_10ma = True
            closed_below_on_day = offset

    fully_evaluated = eval_n >= milestone_days
    survives_day7 = (
        fully_evaluated and not broke_entry_low and not closed_below_10ma
    )

    return TraceEntry(
        tool=TOOL,
        inputs={
            "entry_date": entry_d.isoformat(),
            "entry_low": entry_low,
            "intraday_low_check": intraday_low_check,
            "milestone_days": milestone_days,
            "trail_ma_period": trail_ma_period,
            "rows": len(df),
        },
        output={
            "survives_day7": survives_day7,
            "fully_evaluated": fully_evaluated,
            "trading_days_since_entry": eval_n,
            "broke_entry_low": broke_entry_low,
            "broke_entry_low_on_day": broke_low_on_day,
            "closed_below_10ma": closed_below_10ma,
            "closed_below_10ma_on_day": closed_below_on_day,
            "entry_low_used": entry_low_used,
            "milestone_window_dates": window_dates,
        },
    )


def compute_from_ticker(
    ticker: str,
    entry_date: str | date,
    entry_low: float | None = None,
    intraday_low_check: bool = True,
) -> TraceEntry:
    fetch = fetch_ohlcv(ticker, period="3mo")
    entry = compute_from_ohlcv(
        fetch.df,
        entry_date=entry_date,
        entry_low=entry_low,
        intraday_low_check=intraday_low_check,
    )
    entry.inputs = {
        **entry.inputs,
        "ticker": ticker,
        "source": fetch.source,
        "data_fetched_at": fetch.fetched_at,
    }
    return entry


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.day7_milestone_check",
        description="Did the position survive 7 trading days post-entry?",
    )
    p.add_argument("ticker")
    p.add_argument("--entry-date", required=True, help="ISO date e.g. 2026-05-17")
    p.add_argument("--entry-low", type=float, default=None)
    p.add_argument(
        "--close-low-only",
        action="store_true",
        help="Use close-below-entry-low check instead of intraday low.",
    )
    args = p.parse_args()
    emit(
        compute_from_ticker(
            ticker=args.ticker,
            entry_date=args.entry_date,
            entry_low=args.entry_low,
            intraday_low_check=not args.close_low_only,
        )
    )


if __name__ == "__main__":
    main()
