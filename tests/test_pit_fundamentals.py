"""Tests for the PIT fundamentals layer — anti-lookahead + TTM derivation.

All offline: synthetic company-facts dicts, no network.
"""
from __future__ import annotations

from datetime import date

import pytest

from tools.fundamentals import pit_fundamentals as pf


def _usd_point(val, end, filed, start=None, form="10-Q", fp="Q1"):
    d = {"val": val, "end": end, "filed": filed, "form": form, "fp": fp,
         "accn": "x", "fy": 2023}
    if start:
        d["start"] = start
    return d


def _facts(units_by_concept):
    """units_by_concept: {(ns, concept): {unit: [rows]}}"""
    facts = {"facts": {}}
    for (ns, concept), units in units_by_concept.items():
        facts["facts"].setdefault(ns, {})[concept] = {"units": units}
    return facts


# --------------------------------------------------------------------------- #
# extract_points + fallback chain                                             #
# --------------------------------------------------------------------------- #
def test_extract_points_first_chain_with_data_wins():
    facts = _facts({
        ("us-gaap", "StockholdersEquity"): {"USD": [
            _usd_point(100.0, "2023-03-31", "2023-04-30", form="10-Q")]},
    })
    pts = pf.extract_points(facts, pf.BOOK_EQUITY)
    assert len(pts) == 1 and pts[0].val == 100.0
    assert pts[0].concept == "us-gaap:StockholdersEquity"


def test_extract_points_falls_through_to_second_concept():
    facts = _facts({
        ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"):
            {"USD": [_usd_point(250.0, "2023-03-31", "2023-04-30")]},
    })
    pts = pf.extract_points(facts, pf.BOOK_EQUITY)
    assert len(pts) == 1 and pts[0].val == 250.0


def test_extract_points_skips_malformed_rows():
    facts = _facts({("us-gaap", "Assets"): {"USD": [
        {"val": None, "end": "2023-03-31", "filed": "2023-04-30"},
        {"val": 5.0, "end": None, "filed": "2023-04-30"},
        _usd_point(900.0, "2023-03-31", "2023-04-30"),
    ]}})
    pts = pf.extract_points(facts, pf.ASSETS)
    assert [p.val for p in pts] == [900.0]


# --------------------------------------------------------------------------- #
# latest_stock_as_of — point-in-time                                          #
# --------------------------------------------------------------------------- #
def test_latest_stock_respects_filed_date():
    pts = pf.extract_points(_facts({("us-gaap", "Assets"): {"USD": [
        _usd_point(100.0, "2023-03-31", "2023-04-30"),
        _usd_point(200.0, "2023-06-30", "2023-07-31"),  # filed AFTER asof
    ]}}), pf.ASSETS)
    # asof before the Q2 filing → only Q1 value visible
    got = pf.latest_stock_as_of(pts, date(2023, 6, 15))
    assert got is not None and got.val == 100.0
    # asof after the Q2 filing → newer value
    got2 = pf.latest_stock_as_of(pts, date(2023, 8, 1))
    assert got2.val == 200.0


def test_latest_stock_none_when_nothing_filed_yet():
    pts = pf.extract_points(_facts({("us-gaap", "Assets"): {"USD": [
        _usd_point(100.0, "2023-03-31", "2023-04-30")]}}), pf.ASSETS)
    assert pf.latest_stock_as_of(pts, date(2023, 1, 1)) is None


def test_latest_stock_uses_restatement_only_after_it_is_filed():
    # original Q1 filed Apr; restated Q1 (same end) filed Sep
    pts = pf.extract_points(_facts({("us-gaap", "StockholdersEquity"): {"USD": [
        _usd_point(100.0, "2023-03-31", "2023-04-30"),
        _usd_point(120.0, "2023-03-31", "2023-09-30"),  # restatement
    ]}}), pf.BOOK_EQUITY)
    assert pf.latest_stock_as_of(pts, date(2023, 6, 1)).val == 100.0   # as-filed
    assert pf.latest_stock_as_of(pts, date(2023, 10, 1)).val == 120.0  # after restatement


# --------------------------------------------------------------------------- #
# ttm_flow_as_of                                                              #
# --------------------------------------------------------------------------- #
def _quarter(val, start, end, filed):
    return _usd_point(val, end, filed, start=start, form="10-Q")


def test_ttm_sums_four_clean_quarters():
    rows = [
        _quarter(10.0, "2022-04-01", "2022-06-30", "2022-07-30"),
        _quarter(11.0, "2022-07-01", "2022-09-30", "2022-10-30"),
        _quarter(12.0, "2022-10-01", "2022-12-31", "2023-01-30"),
        _quarter(13.0, "2023-01-01", "2023-03-31", "2023-04-30"),
    ]
    pts = pf.extract_points(_facts({("us-gaap", "NetIncomeLoss"): {"USD": rows}}), pf.NET_INCOME)
    ttm = pf.ttm_flow_as_of(pts, date(2023, 6, 1))
    assert ttm is not None and ttm.val == pytest.approx(46.0)
    assert ttm.fp == "TTM"


def test_ttm_excludes_quarter_filed_after_asof():
    rows = [
        _quarter(10.0, "2022-04-01", "2022-06-30", "2022-07-30"),
        _quarter(11.0, "2022-07-01", "2022-09-30", "2022-10-30"),
        _quarter(12.0, "2022-10-01", "2022-12-31", "2023-01-30"),
        _quarter(13.0, "2023-01-01", "2023-03-31", "2023-04-30"),
        _quarter(99.0, "2023-04-01", "2023-06-30", "2023-07-30"),  # future
    ]
    pts = pf.extract_points(_facts({("us-gaap", "NetIncomeLoss"): {"USD": rows}}), pf.NET_INCOME)
    # asof before the newest quarter is filed → uses the prior four
    ttm = pf.ttm_flow_as_of(pts, date(2023, 5, 1))
    assert ttm.val == pytest.approx(46.0)


def test_ttm_derives_q4_from_annual():
    # three quarters + a full-year 10-K → Q4 derived = FY - (Q1+Q2+Q3)
    rows = [
        _quarter(10.0, "2022-01-01", "2022-03-31", "2022-04-30"),
        _quarter(11.0, "2022-04-01", "2022-06-30", "2022-07-30"),
        _quarter(12.0, "2022-07-01", "2022-09-30", "2022-10-30"),
        _usd_point(50.0, "2022-12-31", "2023-02-28", start="2022-01-01",
                   form="10-K", fp="FY"),
    ]
    pts = pf.extract_points(_facts({("us-gaap", "NetIncomeLoss"): {"USD": rows}}), pf.NET_INCOME)
    ttm = pf.ttm_flow_as_of(pts, date(2023, 3, 15))
    # derived Q4 = 50 - 33 = 17; TTM = 10+11+12+17 = 50 (the full year)
    assert ttm.val == pytest.approx(50.0)


def test_ttm_falls_back_to_latest_annual_when_no_quarters():
    rows = [
        _usd_point(40.0, "2021-12-31", "2022-02-28", start="2021-01-01", form="10-K", fp="FY"),
        _usd_point(55.0, "2022-12-31", "2023-02-28", start="2022-01-01", form="10-K", fp="FY"),
    ]
    pts = pf.extract_points(_facts({("us-gaap", "NetIncomeLoss"): {"USD": rows}}), pf.NET_INCOME)
    ttm = pf.ttm_flow_as_of(pts, date(2023, 6, 1))
    assert ttm.val == pytest.approx(55.0) and ttm.fp == "FY"


def test_ttm_rolling_handles_cumulative_cashflow_reporting():
    # Cash-flow style: cumulative YTD interims (no discrete quarters) + FY.
    # TTM ending 2024-03-31 = FY2023 + H1-2024-cum - H1-2023-cum.
    rows = [
        _usd_point(40.0, "2022-12-31", "2023-02-28", start="2022-01-01", form="10-K", fp="FY"),
        _usd_point(18.0, "2023-06-30", "2023-07-30", start="2023-01-01", form="10-Q", fp="Q2"),  # H1-2023 cum
        _usd_point(50.0, "2023-12-31", "2024-02-28", start="2023-01-01", form="10-K", fp="FY"),
        _usd_point(22.0, "2024-06-30", "2024-07-30", start="2024-01-01", form="10-Q", fp="Q2"),  # H1-2024 cum
    ]
    pts = pf.extract_points(_facts({
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"): {"USD": rows}}), pf.OCF)
    ttm = pf.ttm_flow_as_of(pts, date(2024, 8, 1))
    # FY2023 (50) + H1-2024 (22) - H1-2023 (18) = 54
    assert ttm is not None and ttm.val == pytest.approx(54.0)
    assert ttm.end == date(2024, 6, 30)


def test_ttm_none_when_nothing_available():
    rows = [_usd_point(40.0, "2023-12-31", "2024-02-28", start="2023-01-01", form="10-K", fp="FY")]
    pts = pf.extract_points(_facts({("us-gaap", "NetIncomeLoss"): {"USD": rows}}), pf.NET_INCOME)
    assert pf.ttm_flow_as_of(pts, date(2023, 1, 1)) is None


# --------------------------------------------------------------------------- #
# fundamentals_as_of — end to end with injected facts                         #
# --------------------------------------------------------------------------- #
def _full_facts():
    q = lambda v, s, e, f: _quarter(v, s, e, f)  # noqa: E731
    return _facts({
        ("us-gaap", "StockholdersEquity"): {"USD": [
            _usd_point(1000.0, "2023-03-31", "2023-04-30")]},
        ("dei", "EntityCommonStockSharesOutstanding"): {"shares": [
            _usd_point(500.0, "2023-03-31", "2023-04-30")]},
        ("us-gaap", "Assets"): {"USD": [_usd_point(3000.0, "2023-03-31", "2023-04-30")]},
        ("us-gaap", "NetIncomeLoss"): {"USD": [
            q(10, "2022-04-01", "2022-06-30", "2022-07-30"),
            q(11, "2022-07-01", "2022-09-30", "2022-10-30"),
            q(12, "2022-10-01", "2022-12-31", "2023-01-30"),
            q(13, "2023-01-01", "2023-03-31", "2023-04-30")]},
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"): {"USD": [
            q(20, "2022-04-01", "2022-06-30", "2022-07-30"),
            q(21, "2022-07-01", "2022-09-30", "2022-10-30"),
            q(22, "2022-10-01", "2022-12-31", "2023-01-30"),
            q(23, "2023-01-01", "2023-03-31", "2023-04-30")]},
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"): {"USD": [
            q(5, "2022-04-01", "2022-06-30", "2022-07-30"),
            q(5, "2022-07-01", "2022-09-30", "2022-10-30"),
            q(5, "2022-10-01", "2022-12-31", "2023-01-30"),
            q(5, "2023-01-01", "2023-03-31", "2023-04-30")]},
    })


def test_fundamentals_as_of_composes_all_fields():
    f = pf.fundamentals_as_of("TEST", date(2023, 6, 1), facts=_full_facts())
    assert f.book_equity == 1000.0
    assert f.shares == 500.0
    assert f.total_assets == 3000.0
    assert f.ttm_net_income == pytest.approx(46.0)
    assert f.ttm_ocf == pytest.approx(86.0)
    assert f.ttm_capex == pytest.approx(20.0)
    assert f.fcf == pytest.approx(66.0)
    assert "book_equity" not in f.provenance  # provenance keyed by concept name
    assert f.provenance["StockholdersEquity"]["filed"] == "2023-04-30"


def test_fundamentals_as_of_fcf_none_when_capex_missing():
    facts = _full_facts()
    del facts["facts"]["us-gaap"]["PaymentsToAcquirePropertyPlantAndEquipment"]
    f = pf.fundamentals_as_of("TEST", date(2023, 6, 1), facts=facts)
    assert f.ttm_ocf is not None and f.ttm_capex is None and f.fcf is None


def test_fundamentals_as_of_before_any_filing_is_all_none():
    f = pf.fundamentals_as_of("TEST", date(2020, 1, 1), facts=_full_facts())
    assert f.book_equity is None and f.ttm_net_income is None and f.fcf is None


# --------------------------------------------------------------------------- #
# ticker -> CIK                                                               #
# --------------------------------------------------------------------------- #
def test_ticker_cik_map_parses_and_zero_pads(tmp_path):
    raw = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"},
           "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft"}}
    m = pf.load_ticker_cik_map(fetcher=lambda: raw, cache_path=tmp_path / "m.json")
    assert m["AAPL"] == "0000320193"
    assert m["MSFT"] == "0000789019"
