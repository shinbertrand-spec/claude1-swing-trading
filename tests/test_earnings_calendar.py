"""Tests for tools.earnings_calendar — pure helpers only.

The yfinance fetch path is exercised manually via CLI. The pure
``_trading_days_between`` and ``_parse_next_earnings_date`` helpers are
testable without network.
"""
from __future__ import annotations

from datetime import date
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
    """Mock a yfinance Ticker whose .calendar returns the dict shape."""
    future = date(2026, 8, 13)
    past = date(2026, 2, 13)
    fake = SimpleNamespace(
        calendar={"Earnings Date": [past, future]},
        earnings_dates=None,
    )
    today = date(2026, 5, 18)
    # Patch the "today" reference by monkey-patching is not necessary —
    # the helper takes today from the system clock; instead we verify the
    # parser returns the future date (whichever is >= today).
    # Since the actual call uses utc today, we simulate by ensuring the
    # future date is in the future relative to "real" today (2026-05-18+).
    parsed, source = _parse_next_earnings_date(fake)
    assert parsed == future
    assert "calendar" in source


def test_parse_from_earnings_dates_df():
    """Mock the DataFrame fallback path."""
    future = pd.Timestamp("2026-08-13")
    past = pd.Timestamp("2026-02-13")
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
