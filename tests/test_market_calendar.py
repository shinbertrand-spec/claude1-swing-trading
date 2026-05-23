"""Tests for tools.market_calendar — NYSE/NASDAQ closure detection."""
from __future__ import annotations

from datetime import date

import pytest

from tools.market_calendar import (
    US_MARKET_HOLIDAYS,
    compute,
    is_market_open_today_et,
)


def test_memorial_day_2026_is_closed():
    entry = compute(date(2026, 5, 25))
    assert entry.output["is_closed"] is True
    assert entry.output["is_holiday"] is True
    assert entry.output["holiday_name"] == "Memorial Day"
    assert entry.output["is_weekend"] is False
    assert entry.output["next_trading_day"] == "2026-05-26"


def test_day_after_memorial_day_is_open():
    entry = compute(date(2026, 5, 26))
    assert entry.output["is_closed"] is False
    assert entry.output["reason"] == "Open"
    assert entry.output["next_trading_day"] == "2026-05-27"


def test_saturday_is_weekend_not_holiday():
    entry = compute(date(2026, 5, 23))  # Sat
    assert entry.output["is_closed"] is True
    assert entry.output["is_weekend"] is True
    assert entry.output["is_holiday"] is False
    assert entry.output["holiday_name"] is None
    assert entry.output["reason"] == "Weekend"
    assert entry.output["next_trading_day"] == "2026-05-26"  # skips Sun + Memorial Day


def test_sunday_skips_to_tuesday_after_memorial_day_weekend():
    entry = compute(date(2026, 5, 24))  # Sun before Memorial Day
    assert entry.output["is_weekend"] is True
    assert entry.output["next_trading_day"] == "2026-05-26"  # not Mon (Memorial Day)


def test_observed_holiday_when_jul_4_is_saturday():
    # 2026-07-04 is Sat → market observes on Fri 2026-07-03
    entry = compute(date(2026, 7, 3))
    assert entry.output["is_holiday"] is True
    assert "Independence Day" in entry.output["holiday_name"]


def test_regular_weekday_is_open():
    # Wed 2026-04-29 — no holiday near
    entry = compute(date(2026, 4, 29))
    assert entry.output["is_closed"] is False
    assert entry.output["reason"] == "Open"


def test_out_of_data_flag_after_2027_max():
    max_known = max(US_MARKET_HOLIDAYS)
    far_future = date(max_known.year + 2, 6, 15)  # mid-year past the table
    entry = compute(far_future)
    assert entry.output["out_of_data"] is True


def test_2027_good_friday_correct():
    # Good Friday 2027 = March 26
    entry = compute(date(2027, 3, 26))
    assert entry.output["is_holiday"] is True
    assert entry.output["holiday_name"] == "Good Friday"


def test_trace_entry_shape():
    entry = compute(date(2026, 5, 25))
    assert entry.tool == "tools/market_calendar.py"
    assert entry.inputs == {"check_date": "2026-05-25"}
    assert "fetched_at" in entry.to_dict()
    assert entry.output["date"] == "2026-05-25"


def test_is_market_open_today_et_returns_bool():
    # Smoke test — just confirm the convenience wrapper returns a bool
    result = is_market_open_today_et()
    assert isinstance(result, bool)
