"""Earnings-calendar fetch — next earnings date + trading days to.

Used for:

* Hard rule: no entries within 10 trading days of earnings (unless EP).
* EP setup: compute the ``mandatory_exit_date`` per
  ``swing-earnings-pivot.md`` (exit before next earnings).

Returns the **primary** source (yfinance). Per ``trade-researcher`` working
principle #1, earnings dates must be verified against TWO independent
sources before the agent acts on one — the secondary source check is a
separate concern handled by the caller (typically WebSearch in
trade-researcher).

CLI::

    uv run python -m tools.earnings_calendar AAPL
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from .cli import emit
from .contract import TraceEntry

TOOL = "tools/earnings_calendar.py"
EARNINGS_BLACKOUT_DAYS = 10  # CLAUDE.md hard rule


def _trading_days_between(today: date, target: date) -> int:
    """Return business-day count between today and target (target excluded
    if same day, included otherwise). Uses numpy busday_count which
    excludes weekends. Does not factor in market holidays — Phase 2
    baseline; refine in Phase 3 staleness work if precision matters.
    """
    if target < today:
        return -int(np.busday_count(target, today))
    return int(np.busday_count(today, target))


def _parse_next_earnings_date(ticker_obj) -> tuple[date | None, str]:
    """Extract next earnings date from a yfinance Ticker.

    yfinance exposes earnings dates via ``.calendar`` (dict) and
    ``.earnings_dates`` (DataFrame). Both shapes occur depending on the
    underlying Yahoo response. Returns (date, source_field) or (None, reason).
    """
    today = datetime.now(timezone.utc).date()

    # Try .calendar first (typical for single-stock queries).
    try:
        cal = ticker_obj.calendar
    except Exception as exc:
        cal = None
        cal_err = str(exc)
    else:
        cal_err = None
    if isinstance(cal, dict):
        eds = cal.get("Earnings Date")
        if eds:
            # Yahoo often returns a list of one or two dates.
            candidates = [
                d if isinstance(d, date) else pd.Timestamp(d).date()
                for d in (eds if isinstance(eds, (list, tuple)) else [eds])
            ]
            future = [d for d in candidates if d >= today]
            if future:
                return min(future), "calendar.Earnings Date"

    # Fallback: .earnings_dates DataFrame.
    try:
        ed_df = ticker_obj.earnings_dates
    except Exception as exc:
        return None, f"both fetch paths failed; calendar_err={cal_err}; earnings_dates_err={exc}"
    if ed_df is None or ed_df.empty:
        return None, "no earnings dates returned"
    idx_dates = [pd.Timestamp(ts).date() for ts in ed_df.index]
    future = [d for d in idx_dates if d >= today]
    if future:
        return min(future), "earnings_dates index"
    return None, "no future earnings dates found"


def compute_from_ticker(ticker: str) -> TraceEntry:
    """Fetch next earnings date via yfinance.

    Returns TraceEntry with output:
        - next_earnings_date: ISO-date string or None
        - trading_days_to_earnings: int or None
        - within_blackout_window: bool (True iff ≤10 trading days)
        - source_field: which yfinance field yielded the result (or
          failure reason)
    """
    import yfinance as yf

    t = yf.Ticker(ticker)
    next_date, source_field = _parse_next_earnings_date(t)
    today = datetime.now(timezone.utc).date()
    tdays = _trading_days_between(today, next_date) if next_date else None
    within_blackout = (
        tdays is not None and 0 <= tdays <= EARNINGS_BLACKOUT_DAYS
    )
    return TraceEntry(
        tool=TOOL,
        inputs={"ticker": ticker, "today": today.isoformat()},
        output={
            "next_earnings_date": next_date.isoformat() if next_date else None,
            "trading_days_to_earnings": tdays,
            "within_blackout_window": within_blackout,
            "blackout_threshold_days": EARNINGS_BLACKOUT_DAYS,
            "source_field": source_field,
            "source": f"yfinance:{ticker}",
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.earnings_calendar",
        description="Next earnings date + trading days to + blackout check.",
    )
    p.add_argument("ticker")
    args = p.parse_args()
    emit(compute_from_ticker(args.ticker))


if __name__ == "__main__":
    main()
