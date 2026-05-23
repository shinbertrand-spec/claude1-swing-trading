"""US market calendar — NYSE/NASDAQ closures.

Used to gate cron-driven slash commands (``/news-hourly``,
``/morning-scan-telegram``) so they don't fire on US market holidays. The
``/eod-journal`` command can also use this to skip closed days.

Holiday list is hardcoded per the NYSE published schedule. Updated through
2027. Add new years as they're published.

Early-close days (1pm ET, e.g. day after Thanksgiving) are NOT modelled in
Phase 1 — the snapshot pipeline treats early-close days as regular trading
days. Refine in Phase 2 if precision matters.

CLI::

    uv run python -m tools.market_calendar 2026-05-25
    # → {"is_closed": true, "reason": "Memorial Day", ...}
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone

from .cli import emit
from .contract import TraceEntry

TOOL = "tools/market_calendar.py"

# NYSE / NASDAQ full-day closures. Date → human-readable reason.
# Source: nyse.com/markets/hours-calendars
US_MARKET_HOLIDAYS: dict[date, str] = {
    # 2026
    date(2026, 1, 1):   "New Year's Day",
    date(2026, 1, 19):  "Martin Luther King Jr. Day",
    date(2026, 2, 16):  "Washington's Birthday",
    date(2026, 4, 3):   "Good Friday",
    date(2026, 5, 25):  "Memorial Day",
    date(2026, 6, 19):  "Juneteenth National Independence Day",
    date(2026, 7, 3):   "Independence Day (observed; Jul 4 is Saturday)",
    date(2026, 9, 7):   "Labor Day",
    date(2026, 11, 26): "Thanksgiving Day",
    date(2026, 12, 25): "Christmas Day",
    # 2027
    date(2027, 1, 1):   "New Year's Day",
    date(2027, 1, 18):  "Martin Luther King Jr. Day",
    date(2027, 2, 15):  "Washington's Birthday",
    date(2027, 3, 26):  "Good Friday",
    date(2027, 5, 31):  "Memorial Day",
    date(2027, 6, 18):  "Juneteenth (observed; Jun 19 is Saturday)",
    date(2027, 7, 5):   "Independence Day (observed; Jul 4 is Sunday)",
    date(2027, 9, 6):   "Labor Day",
    date(2027, 11, 25): "Thanksgiving Day",
    date(2027, 12, 24): "Christmas Day (observed; Dec 25 is Saturday)",
}


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def _next_trading_day(d: date) -> date:
    candidate = d + timedelta(days=1)
    while _is_weekend(candidate) or candidate in US_MARKET_HOLIDAYS:
        candidate += timedelta(days=1)
    return candidate


def compute(check_date: date) -> TraceEntry:
    """Return whether the US equity market is closed on ``check_date``.

    Output fields:
        is_closed: True if weekend OR holiday OR after 2027 (out-of-data).
        reason: short string. "Weekend", a holiday name, or "Open".
        is_weekend: bool.
        is_holiday: bool.
        holiday_name: optional str; populated only when is_holiday.
        next_trading_day: ISO date of the next open session.
        out_of_data: True if check_date is past the hardcoded table.
    """
    weekend = _is_weekend(check_date)
    holiday_name = US_MARKET_HOLIDAYS.get(check_date)
    is_holiday = holiday_name is not None
    is_closed = weekend or is_holiday

    # Past the hardcoded table → flag for the caller. We still answer the
    # weekend question correctly (deterministic), but holidays beyond the
    # table are unknown.
    max_known = max(US_MARKET_HOLIDAYS)
    out_of_data = check_date > max_known

    reason = "Open"
    if weekend:
        reason = "Weekend"
    elif holiday_name:
        reason = holiday_name

    output = {
        "date": check_date.isoformat(),
        "is_closed": is_closed,
        "reason": reason,
        "is_weekend": weekend,
        "is_holiday": is_holiday,
        "holiday_name": holiday_name,
        "next_trading_day": _next_trading_day(check_date).isoformat(),
        "out_of_data": out_of_data,
    }

    return TraceEntry(
        tool=TOOL,
        inputs={"check_date": check_date.isoformat()},
        output=output,
    )


def compute_today_et() -> TraceEntry:
    """Convenience: compute against today's US/Eastern date."""
    from zoneinfo import ZoneInfo
    et_now = datetime.now(ZoneInfo("America/New_York"))
    return compute(et_now.date())


def is_market_open_today_et() -> bool:
    """Boolean shortcut for cron-style callers."""
    return not compute_today_et().output["is_closed"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Check US equity market open/closed status for a date.")
    parser.add_argument(
        "check_date",
        nargs="?",
        default=None,
        help="ISO date (YYYY-MM-DD). Default: today in US/Eastern.",
    )
    args = parser.parse_args()

    if args.check_date:
        d = date.fromisoformat(args.check_date)
        entry = compute(d)
    else:
        entry = compute_today_et()

    emit(entry)


if __name__ == "__main__":
    main()
