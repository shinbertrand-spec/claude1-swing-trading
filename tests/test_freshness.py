"""Tests for tools.freshness."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tools.freshness import (
    StalenessError,
    assert_ledger_fresh,
    audit_ledger,
    check_section,
    is_market_open_at,
    last_market_close_before,
)

ET = ZoneInfo("America/New_York")


def test_market_open_during_session():
    # Wednesday 2026-05-20 12:00 ET = market open.
    et_noon = datetime(2026, 5, 20, 12, 0, tzinfo=ET)
    assert is_market_open_at(et_noon) is True


def test_market_closed_on_weekend():
    # Saturday 2026-05-23 12:00 ET = closed.
    sat = datetime(2026, 5, 23, 12, 0, tzinfo=ET)
    assert is_market_open_at(sat) is False


def test_market_closed_after_hours():
    et_evening = datetime(2026, 5, 20, 18, 0, tzinfo=ET)
    assert is_market_open_at(et_evening) is False


def test_last_market_close_skips_weekend():
    # Sunday afternoon → last close is Friday 16:00 ET.
    sun = datetime(2026, 5, 24, 14, 0, tzinfo=ET)
    last = last_market_close_before(sun)
    last_et = last.astimezone(ET)
    assert last_et.weekday() == 4  # Friday
    assert last_et.hour == 16 and last_et.minute == 0


def test_quote_fresh_during_market_hours_within_4h():
    """Quote fetched 1h ago during market hours → fresh."""
    asof = datetime(2026, 5, 20, 13, 0, tzinfo=ET)
    ledger = {
        "quote": {
            "last": 100.0, "bid": 99.9, "ask": 100.1,
            "session": "regular",
            "fetched_at": (asof - timedelta(hours=1)).isoformat(timespec="seconds"),
        }
    }
    r = check_section(ledger, "quote", asof=asof)
    assert r.status == "fresh"
    assert r.market_was_open is True


def test_quote_stale_during_market_hours_over_4h():
    """Quote fetched 5h ago during market hours → stale."""
    asof = datetime(2026, 5, 20, 14, 0, tzinfo=ET)
    ledger = {
        "quote": {
            "last": 100.0, "bid": 99.9, "ask": 100.1,
            "session": "regular",
            "fetched_at": (asof - timedelta(hours=5)).isoformat(timespec="seconds"),
        }
    }
    r = check_section(ledger, "quote", asof=asof)
    assert r.status == "stale"


def test_quote_fresh_after_hours_if_after_last_close():
    """Sat 12 ET; quote fetched Fri 16:01 ET (after last close) → fresh."""
    asof = datetime(2026, 5, 23, 12, 0, tzinfo=ET)
    last_close = datetime(2026, 5, 22, 16, 1, tzinfo=ET)
    ledger = {
        "quote": {
            "last": 100.0, "bid": 99.9, "ask": 100.1,
            "session": "closed",
            "fetched_at": last_close.isoformat(timespec="seconds"),
        }
    }
    r = check_section(ledger, "quote", asof=asof)
    assert r.status == "fresh"


def test_quote_stale_after_hours_if_before_last_close():
    """Sat 12 ET; quote fetched Thu 16:00 ET → stale (Fri close came after)."""
    asof = datetime(2026, 5, 23, 12, 0, tzinfo=ET)
    thu = datetime(2026, 5, 21, 16, 0, tzinfo=ET)
    ledger = {
        "quote": {
            "last": 100.0, "bid": 99.9, "ask": 100.1,
            "session": "closed",
            "fetched_at": thu.isoformat(timespec="seconds"),
        }
    }
    r = check_section(ledger, "quote", asof=asof)
    assert r.status == "stale"


def test_technical_uses_computed_at_24h():
    asof = datetime(2026, 5, 20, 14, 0, tzinfo=ET)
    ledger = {
        "technical": {
            "trend_template_passes": 8,
            "computed_at": (asof - timedelta(hours=10)).isoformat(timespec="seconds"),
        }
    }
    r = check_section(ledger, "technical", asof=asof)
    assert r.status == "fresh"
    assert r.timestamp_field == "computed_at"


def test_technical_stale_after_24h():
    asof = datetime(2026, 5, 20, 14, 0, tzinfo=ET)
    ledger = {
        "technical": {
            "trend_template_passes": 8,
            "computed_at": (asof - timedelta(hours=30)).isoformat(timespec="seconds"),
        }
    }
    r = check_section(ledger, "technical", asof=asof)
    assert r.status == "stale"


def test_missing_section():
    asof = datetime(2026, 5, 20, 14, 0, tzinfo=ET)
    r = check_section({}, "quote", asof=asof)
    assert r.status == "missing_section"


def test_missing_timestamp():
    asof = datetime(2026, 5, 20, 14, 0, tzinfo=ET)
    ledger = {"technical": {"trend_template_passes": 8}}  # no computed_at
    r = check_section(ledger, "technical", asof=asof)
    assert r.status == "missing_timestamp"


def test_fundamentals_warns_earnings_blackout():
    """Earnings 5 trading days away → warning (not stale)."""
    asof = datetime(2026, 5, 20, 14, 0, tzinfo=ET)
    next_earnings = (asof + timedelta(days=8)).date()
    ledger = {
        "fundamentals": {
            "fetched_at": asof.isoformat(timespec="seconds"),
            "filing_date": (asof.date() - timedelta(days=40)).isoformat(),
            "next_earnings_date": next_earnings.isoformat(),
            "next_earnings_source": "broker_api",
            "next_earnings_source_secondary": "web:nasdaq.com",
        }
    }
    r = check_section(ledger, "fundamentals", asof=asof)
    assert r.status == "fresh"
    assert any("10 trading days" in w for w in r.warnings)


def test_fundamentals_warns_no_secondary_source():
    asof = datetime(2026, 5, 20, 14, 0, tzinfo=ET)
    ledger = {
        "fundamentals": {
            "fetched_at": asof.isoformat(timespec="seconds"),
            "next_earnings_source": "broker_api",
        }
    }
    r = check_section(ledger, "fundamentals", asof=asof)
    assert any("secondary" in w for w in r.warnings)


def test_fundamentals_warns_old_filing_date():
    """filing_date 200 days old → likely missed an earnings cycle."""
    asof = datetime(2026, 5, 20, 14, 0, tzinfo=ET)
    ledger = {
        "fundamentals": {
            "fetched_at": asof.isoformat(timespec="seconds"),
            "filing_date": (asof.date() - timedelta(days=200)).isoformat(),
            "next_earnings_source": "broker_api",
            "next_earnings_source_secondary": "web:nasdaq.com",
        }
    }
    r = check_section(ledger, "fundamentals", asof=asof)
    assert any("filing_date" in w for w in r.warnings)


def test_catalyst_7_day_window():
    asof = datetime(2026, 5, 20, 14, 0, tzinfo=ET)
    ledger = {
        "catalyst": {
            "type": "earnings",
            "verified": True,
            "fetched_at": (asof - timedelta(days=10)).isoformat(timespec="seconds"),
        }
    }
    r = check_section(ledger, "catalyst", asof=asof)
    assert r.status == "stale"

    ledger["catalyst"]["fetched_at"] = (asof - timedelta(days=3)).isoformat(timespec="seconds")
    r2 = check_section(ledger, "catalyst", asof=asof)
    assert r2.status == "fresh"


def test_audit_ledger_aggregates():
    asof = datetime(2026, 5, 20, 14, 0, tzinfo=ET)
    ledger = {
        "quote": {
            "last": 100.0, "bid": 99.9, "ask": 100.1, "session": "regular",
            "fetched_at": (asof - timedelta(hours=1)).isoformat(timespec="seconds"),
        },
        "technical": {
            "trend_template_passes": 8,
            "computed_at": (asof - timedelta(hours=30)).isoformat(timespec="seconds"),
        },
    }
    report = audit_ledger(ledger, asof=asof)
    assert report.overall == "stale"
    assert "technical" in report.stale_sections
    assert "quote" not in report.stale_sections


def test_assert_ledger_fresh_raises_on_stale():
    asof = datetime(2026, 5, 20, 14, 0, tzinfo=ET)
    ledger = {
        "quote": {
            "last": 100.0, "bid": 99.9, "ask": 100.1, "session": "regular",
            "fetched_at": (asof - timedelta(hours=10)).isoformat(timespec="seconds"),
        }
    }
    with pytest.raises(StalenessError, match="stale"):
        assert_ledger_fresh(ledger, asof=asof)


def test_assert_ledger_fresh_passes_on_fresh():
    asof = datetime(2026, 5, 20, 14, 0, tzinfo=ET)
    ledger = {
        "quote": {
            "last": 100.0, "bid": 99.9, "ask": 100.1, "session": "regular",
            "fetched_at": (asof - timedelta(hours=1)).isoformat(timespec="seconds"),
        }
    }
    report = assert_ledger_fresh(ledger, asof=asof)
    assert report.is_fresh is True


def test_parse_iso_with_z_suffix():
    """ISO with trailing 'Z' parses correctly."""
    asof = datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc)
    ledger = {
        "quote": {
            "last": 100.0, "bid": 99.9, "ask": 100.1, "session": "regular",
            "fetched_at": "2026-05-20T13:00:00Z",
        }
    }
    r = check_section(ledger, "quote", asof=asof)
    assert r.status == "fresh"
