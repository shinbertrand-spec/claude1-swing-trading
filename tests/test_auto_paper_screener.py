"""Tests for tools.auto_paper.screener — pre-placement disqualifier checks.

Each check is unit-tested with mocked external calls (finviz HTML +
yfinance Ticker.info + earnings_calendar). Plus a GO-specific regression
test against the real class-action headlines that were live as of
2026-05-27 (mocked into the headline list).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.auto_paper import screener
from tools.contract import TraceEntry


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------


# Drawn from the actual finviz news panel for GO on 2026-05-27 — the three
# "GO Lawsuit Alleges..." headlines pulled by the live finviz scrape.
_GO_HEADLINES_WITH_LITIGATION = [
    "Grocery Outlet Holding Corp. Q1 2026 Earnings Call Summary",
    "GO Lawsuit Alleges Misrepresentations Regarding Unsustainable Retail Store Expansion - Grocery",
    "GO Lawsuit Alleges Inadequate Risk Disclosures Regarding Financial Performance",
    "GO Lawsuit Alleges Concealment of Deteriorating Business Performance Metrics",
    "Grocery Outlet Holding Corp. Adds Two New Independent Directors",
]

_CLEAN_HEADLINES = [
    "NVDA Hits New All-Time High On Strong AI Demand",
    "Analysts Raise Price Targets After Q1 Beat",
    "CEO Discusses Outlook on Earnings Call",
]

_DILUTION_HEADLINES = [
    "ACME Corp Announces Pricing of $250M Common Stock Offering",
    "ACME Closes Secondary Offering at $42.50",
    "Other unrelated news",
]


# Stub for the earnings_calendar TraceEntry shape.
def _earnings_entry(
    *, within_blackout: bool, days_to: int | None = 30, date_str: str = "2026-08-20"
) -> TraceEntry:
    return TraceEntry(
        tool="tools/earnings_calendar.py",
        inputs={"ticker": "MOCK", "today": "2026-05-27"},
        output={
            "next_earnings_date": date_str,
            "trading_days_to_earnings": days_to,
            "within_blackout_window": within_blackout,
            "blackout_threshold_days": 10,
            "source_field": "calendar.Earnings Date",
            "source": "yfinance:MOCK",
        },
    )


# ---------------------------------------------------------------------------
# _check_litigation
# ---------------------------------------------------------------------------


def test_litigation_clean_passes():
    r = screener._check_litigation("NVDA", _CLEAN_HEADLINES, None)
    assert r.passed is True
    assert r.evidence["headlines_scanned"] == 3


def test_litigation_class_action_blocks():
    r = screener._check_litigation("GO", _GO_HEADLINES_WITH_LITIGATION, None)
    assert r.passed is False
    assert "litigation" in r.reason.lower() or "sec" in r.reason.lower()
    assert r.evidence["total_matches"] == 3
    # All 3 matched headlines surfaced; cap at 5 in evidence.
    assert len(r.evidence["matching_headlines"]) == 3


def test_litigation_fetch_error_fails_open():
    r = screener._check_litigation("XYZ", [], "finviz HTTPError after retry: 429")
    assert r.passed is True
    assert r.evidence["fail_mode"] == "fail_open"
    assert "429" in r.evidence["fetch_error"]


def test_litigation_alternative_phrasings_match():
    # Each phrasing should independently trip the litigation pattern.
    cases = [
        "ABC stockholder files SEC investigation complaint",
        "Investor Alert: ABC Securities Lawsuit Filed",
        "ABC Faces Class Action Over Misleading Statements",
        "Shareholder Investigation of ABC Continues",
        "ABC Lawsuit Alleges Material Misstatements",
    ]
    for headline in cases:
        r = screener._check_litigation("ABC", [headline], None)
        assert r.passed is False, f"failed to flag: {headline!r}"


# ---------------------------------------------------------------------------
# _check_dilution
# ---------------------------------------------------------------------------


def test_dilution_clean_passes():
    r = screener._check_dilution("NVDA", _CLEAN_HEADLINES, None)
    assert r.passed is True


def test_dilution_offering_blocks():
    r = screener._check_dilution("ACME", _DILUTION_HEADLINES, None)
    assert r.passed is False
    assert r.evidence["total_matches"] == 2


def test_dilution_fetch_error_fails_open():
    r = screener._check_dilution("XYZ", [], "network timeout")
    assert r.passed is True
    assert r.evidence["fail_mode"] == "fail_open"


# ---------------------------------------------------------------------------
# _check_earnings_blackout
# ---------------------------------------------------------------------------


def test_earnings_blackout_outside_window_passes(monkeypatch):
    monkeypatch.setattr(
        screener, "earnings_from_ticker",
        lambda t: _earnings_entry(within_blackout=False, days_to=45),
    )
    r = screener._check_earnings_blackout("MOCK")
    assert r.passed is True
    assert r.evidence["trading_days_to_earnings"] == 45


def test_earnings_blackout_inside_window_blocks(monkeypatch):
    monkeypatch.setattr(
        screener, "earnings_from_ticker",
        lambda t: _earnings_entry(
            within_blackout=True, days_to=4, date_str="2026-06-02",
        ),
    )
    r = screener._check_earnings_blackout("MOCK")
    assert r.passed is False
    assert "4" in r.reason
    assert "2026-06-02" in r.reason


def test_earnings_blackout_tool_error_fails_open(monkeypatch):
    def _raises(t):
        raise RuntimeError("yfinance unavailable")
    monkeypatch.setattr(screener, "earnings_from_ticker", _raises)
    r = screener._check_earnings_blackout("MOCK")
    assert r.passed is True
    assert r.evidence["fail_mode"] == "fail_open"


# ---------------------------------------------------------------------------
# _lookup_sector
# ---------------------------------------------------------------------------


class _FakeTicker:
    def __init__(self, sector, industry="Whatever"):
        self.info = {"sector": sector, "industry": industry}


def test_sector_lookup_no_mismatch(monkeypatch):
    monkeypatch.setattr(
        screener, "__name__", screener.__name__,
    )  # no-op; explicit so the next line's import path is clear

    import sys
    fake_yf = SimpleNamespace(Ticker=lambda t: _FakeTicker("Technology"))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    r = screener._lookup_sector("NVDA", "XLK")
    assert r.passed is True
    assert r.evidence["mismatch"] is False
    assert r.evidence["actual_sector_etf"] == "XLK"


def test_sector_lookup_detects_mismatch(monkeypatch):
    import sys
    fake_yf = SimpleNamespace(Ticker=lambda t: _FakeTicker("Industrials"))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    r = screener._lookup_sector("VRT", "XLK")
    assert r.passed is True  # never blocks
    assert r.evidence["mismatch"] is True
    assert r.evidence["actual_sector_etf"] == "XLI"
    assert r.evidence["claimed_sector_etf"] == "XLK"


def test_sector_lookup_unknown_sector_yields_no_correction(monkeypatch):
    import sys
    fake_yf = SimpleNamespace(Ticker=lambda t: _FakeTicker("MadeUpSector"))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    r = screener._lookup_sector("XYZ", "XLK")
    assert r.passed is True
    assert r.evidence["actual_sector_etf"] is None
    assert r.evidence["mismatch"] is False


def test_sector_lookup_fetch_error_fails_open(monkeypatch):
    import sys
    def _raises(t):
        raise RuntimeError("yfinance down")
    fake_yf = SimpleNamespace(Ticker=_raises)
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    r = screener._lookup_sector("XYZ", "XLK")
    assert r.passed is True
    assert r.evidence["fail_mode"] == "fail_open"


# ---------------------------------------------------------------------------
# Top-level screen() composition
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_clean_fetch(monkeypatch):
    """All checks pass — clean baseline name."""
    monkeypatch.setattr(
        screener, "_fetch_finviz_news_headlines",
        lambda t: (_CLEAN_HEADLINES, None),
    )
    monkeypatch.setattr(
        screener, "earnings_from_ticker",
        lambda t: _earnings_entry(within_blackout=False, days_to=45),
    )
    import sys
    fake_yf = SimpleNamespace(Ticker=lambda t: _FakeTicker("Technology"))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)


@pytest.fixture
def stub_go_fetch(monkeypatch):
    """GO regression — active class action mocked into finviz response."""
    monkeypatch.setattr(
        screener, "_fetch_finviz_news_headlines",
        lambda t: (_GO_HEADLINES_WITH_LITIGATION, None),
    )
    monkeypatch.setattr(
        screener, "earnings_from_ticker",
        lambda t: _earnings_entry(within_blackout=False, days_to=50),
    )
    import sys
    fake_yf = SimpleNamespace(Ticker=lambda t: _FakeTicker("Consumer Defensive"))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)


def test_screen_clean_passes(stub_clean_fetch):
    r = screener.screen("NVDA", claimed_sector_etf="XLK")
    assert r.blocked is False
    assert r.blocking_checks == []
    assert r.corrected_sector_etf is None
    assert len(r.checks) == 4


def test_screen_go_regression_blocks(stub_go_fetch):
    """The actual GO case: active class action + XLK heuristic mistag."""
    r = screener.screen("GO", claimed_sector_etf="XLK")
    assert r.blocked is True
    assert "litigation" in r.blocking_checks
    # GO's real sector is XLP (Consumer Defensive). The scanner had it as XLK.
    assert r.corrected_sector_etf == "XLP"


def test_screen_multiple_blocks_surface_all(monkeypatch):
    """Litigation + earnings blackout both fire → both in blocking_checks."""
    monkeypatch.setattr(
        screener, "_fetch_finviz_news_headlines",
        lambda t: (_GO_HEADLINES_WITH_LITIGATION, None),
    )
    monkeypatch.setattr(
        screener, "earnings_from_ticker",
        lambda t: _earnings_entry(within_blackout=True, days_to=5),
    )
    import sys
    fake_yf = SimpleNamespace(Ticker=lambda t: _FakeTicker("Technology"))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    r = screener.screen("XYZ", claimed_sector_etf="XLK")
    assert r.blocked is True
    assert set(r.blocking_checks) == {"litigation", "earnings_blackout"}


def test_screen_sector_only_correction_does_not_block(monkeypatch):
    """Sector-mismatch alone is advisory, never a hard block."""
    monkeypatch.setattr(
        screener, "_fetch_finviz_news_headlines",
        lambda t: (_CLEAN_HEADLINES, None),
    )
    monkeypatch.setattr(
        screener, "earnings_from_ticker",
        lambda t: _earnings_entry(within_blackout=False, days_to=45),
    )
    import sys
    fake_yf = SimpleNamespace(Ticker=lambda t: _FakeTicker("Industrials"))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    r = screener.screen("VRT", claimed_sector_etf="XLK")
    assert r.blocked is False
    assert r.blocking_checks == []
    assert r.corrected_sector_etf == "XLI"


def test_screen_trace_entry_shape(stub_clean_fetch):
    entry = screener.screen_as_trace_entry("NVDA", claimed_sector_etf="XLK")
    assert entry.tool == "tools/auto_paper/screener.py"
    assert entry.inputs["ticker"] == "NVDA"
    assert entry.output["blocked"] is False
    assert len(entry.output["checks"]) == 4
