"""Tests for tools.earnings_calendar — pure helpers only.

The yfinance fetch path is exercised manually via CLI. The pure
``_trading_days_between`` and ``_parse_next_earnings_date`` helpers are
testable without network.
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from tools.earnings_calendar import _parse_next_earnings_date, _trading_days_between


def test_trading_days_between_same_week():
    # Monday to Friday = 4 trading days (5 weekdays exclusive of start).
    assert _trading_days_between(date(2026, 5, 18), date(2026, 5, 22)) == 4


def test_trading_days_between_skips_weekend():
    # Friday to next Monday = 1 trading day.
    assert _trading_days_between(date(2026, 5, 22), date(2026, 5, 25)) == 1


def test_trading_days_between_past_date_negative():
    # Target before today → negative.
    n = _trading_days_between(date(2026, 5, 22), date(2026, 5, 15))
    assert n < 0


def test_parse_from_calendar_dict():
    """Mock a yfinance Ticker whose .calendar returns the dict shape.

    The helper compares against the SYSTEM clock, so the fixture dates are
    clock-relative (2026-08-17 fix: the original hardcoded date(2026, 8, 13)
    expired and both parse tests started failing on age alone)."""
    future = date.today() + timedelta(days=88)
    past = date.today() - timedelta(days=95)
    fake = SimpleNamespace(
        calendar={"Earnings Date": [past, future]},
        earnings_dates=None,
    )
    parsed, source = _parse_next_earnings_date(fake)
    assert parsed == future
    assert "calendar" in source


def test_parse_from_earnings_dates_df():
    """Mock the DataFrame fallback path (clock-relative dates, see above)."""
    future = pd.Timestamp(date.today() + timedelta(days=88))
    past = pd.Timestamp(date.today() - timedelta(days=95))
    df = pd.DataFrame(
        {"EPS Estimate": [None, None]},
        index=pd.DatetimeIndex([past, future]),
    )
    fake = SimpleNamespace(
        calendar={},  # empty so we fall through
        earnings_dates=df,
    )
    parsed, source = _parse_next_earnings_date(fake)
    assert parsed == future.date()
    assert "earnings_dates" in source


def test_parse_empty_returns_none():
    fake = SimpleNamespace(calendar={}, earnings_dates=pd.DataFrame())
    parsed, _ = _parse_next_earnings_date(fake)
    assert parsed is None


def test_parse_all_past_returns_none():
    past1 = pd.Timestamp("2024-02-13")
    past2 = pd.Timestamp("2024-08-13")
    df = pd.DataFrame(
        {"x": [0, 0]},
        index=pd.DatetimeIndex([past1, past2]),
    )
    fake = SimpleNamespace(calendar={}, earnings_dates=df)
    parsed, source = _parse_next_earnings_date(fake)
    assert parsed is None
    assert "no future" in source


# ------------------------------------------------- A3 as-of anchoring


def test_parse_as_of_anchors_next_to_simulation_date():
    """With as_of, 'next' means next relative to the simulation date — a
    date that is in the past relative to the wall clock still resolves."""
    d1 = date(2024, 2, 13)
    d2 = date(2024, 8, 13)
    fake = SimpleNamespace(calendar={"Earnings Date": [d1, d2]},
                           earnings_dates=None)
    parsed, _ = _parse_next_earnings_date(fake, as_of=date(2024, 5, 1))
    assert parsed == d2
    parsed_earlier, _ = _parse_next_earnings_date(fake, as_of=date(2024, 1, 1))
    assert parsed_earlier == d1


def test_parse_as_of_after_all_dates_returns_none():
    fake = SimpleNamespace(
        calendar={"Earnings Date": [date(2024, 2, 13)]},
        earnings_dates=None,
    )
    parsed, _ = _parse_next_earnings_date(fake, as_of=date(2024, 12, 1))
    assert parsed is None
