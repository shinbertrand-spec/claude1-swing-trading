"""Tests for tools.thematic_portfolio.kill_switch.clock — RTH session detector."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tools.thematic_portfolio.kill_switch.clock import (
    OFF_HOURS_SLEEP_SECONDS,
    SESSION_SLEEP_SECONDS,
    session_state,
)

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _et_to_utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).astimezone(UTC)


# Tuesday May 26 2026 is a regular trading day (Memorial Day is May 25)
def test_rth_open_at_10am_et_session_cadence():
    now = _et_to_utc(2026, 5, 26, 10, 0)
    s = session_state(now)
    assert s.is_rth_open is True
    assert s.reason == "open"
    assert s.suggested_sleep_seconds == SESSION_SLEEP_SECONDS


def test_rth_open_at_330pm_et_still_session():
    now = _et_to_utc(2026, 5, 26, 15, 30)
    s = session_state(now)
    assert s.is_rth_open is True


def test_rth_closed_exactly_at_4pm_et():
    now = _et_to_utc(2026, 5, 26, 16, 0)
    s = session_state(now)
    assert s.is_rth_open is False
    assert s.reason == "post_close"
    assert s.suggested_sleep_seconds == OFF_HOURS_SLEEP_SECONDS


def test_rth_closed_pre_open_at_9am_et():
    now = _et_to_utc(2026, 5, 26, 9, 0)
    s = session_state(now)
    assert s.is_rth_open is False
    assert s.reason == "pre_open"


def test_rth_opens_exactly_at_930am_et():
    now = _et_to_utc(2026, 5, 26, 9, 30)
    s = session_state(now)
    assert s.is_rth_open is True


def test_saturday_off_hours():
    # Saturday May 23 2026
    now = _et_to_utc(2026, 5, 23, 12, 0)
    s = session_state(now)
    assert s.is_rth_open is False
    assert s.reason == "weekend"
    assert s.suggested_sleep_seconds == OFF_HOURS_SLEEP_SECONDS


def test_sunday_off_hours():
    # Sunday May 24 2026
    now = _et_to_utc(2026, 5, 24, 12, 0)
    s = session_state(now)
    assert s.reason == "weekend"


def test_memorial_day_2026_holiday():
    # Monday May 25 2026 — Memorial Day per market_calendar
    now = _et_to_utc(2026, 5, 25, 12, 0)
    s = session_state(now)
    assert s.is_rth_open is False
    assert s.reason.startswith("holiday:")
    assert "Memorial Day" in s.reason


def test_christmas_day_2026_holiday():
    now = _et_to_utc(2026, 12, 25, 12, 0)
    s = session_state(now)
    assert s.is_rth_open is False
    assert "Christmas" in s.reason


def test_naive_datetime_assumed_utc():
    naive = datetime(2026, 5, 26, 14, 0)  # 14:00 UTC = 10am ET
    s = session_state(naive)
    assert s.is_rth_open is True


def test_default_arg_uses_real_clock():
    # Just verify it doesn't crash and returns a sensible shape
    s = session_state()
    assert s.suggested_sleep_seconds in (SESSION_SLEEP_SECONDS, OFF_HOURS_SLEEP_SECONDS)
    assert isinstance(s.is_rth_open, bool)
