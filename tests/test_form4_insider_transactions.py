"""Tests for tools.fundamentals.form4_insider_transactions — Form 4 ingest.

All tests inject fake Filing-like objects via the ``_filings_factory`` /
``parse_filing`` seams — no live SEC EDGAR calls are made.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from tools.fundamentals.form4_insider_transactions import (
    InsiderPurchase,
    compute,
    ingest_current,
    ingest_day,
    parse_filing,
)


# ---- fakes --------------------------------------------------------------


def _txn(code="P", acq="A", shares=1000, price=10.0, date_="2026-06-12",
         remaining=5000, di="D", footnotes=""):
    return SimpleNamespace(
        transaction_code=code,
        acquired_disposed=acq,
        shares=shares,
        price=price,
        date=date_,
        remaining=remaining,
        direct_indirect=di,
        footnotes=footnotes,
    )


def _owner(name="Jane Insider", cik="0001977231", position="CEO",
           director=True, officer=True, ten_pct=False, other=False,
           officer_title="President and CEO"):
    return SimpleNamespace(
        name=name, cik=cik, position=position,
        is_director=director, is_officer=officer,
        is_ten_pct_owner=ten_pct, is_other=other,
        officer_title=officer_title,
    )


def _form4(txns, *, ticker="ACME", issuer_cik="0001946563",
           issuer_name="Acme Corp", owners=None):
    owners = owners if owners is not None else [_owner()]
    return SimpleNamespace(
        issuer=SimpleNamespace(cik=issuer_cik, name=issuer_name, ticker=ticker),
        reporting_owners=owners,
        non_derivative_table=SimpleNamespace(transactions=txns),
    )


def _filing(form4, *, accession="0000000000-26-000001",
            filing_date="2026-06-15",
            acceptance="2026-06-15 16:50:47"):
    if isinstance(acceptance, str):
        acc_dt = datetime.strptime(acceptance, "%Y-%m-%d %H:%M:%S")
    else:
        acc_dt = acceptance
    return SimpleNamespace(
        accession_no=accession,
        filing_date=filing_date,
        header=SimpleNamespace(acceptance_datetime=acc_dt),
        obj=lambda: form4,
    )


# ---- parse_filing: happy path ------------------------------------------


def test_parse_single_purchase():
    f = _filing(_form4([_txn(shares=7350, price=1.3784)]))
    rows = parse_filing(f)
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, InsiderPurchase)
    assert r.ticker == "ACME"
    assert r.issuer_name == "Acme Corp"
    assert r.insider_name == "Jane Insider"
    assert r.shares == 7350
    assert r.price == pytest.approx(1.3784)
    assert r.value == pytest.approx(round(7350 * 1.3784, 2))
    assert r.is_director is True
    assert r.is_officer is True
    assert r.is_ten_pct_owner is False
    assert r.officer_title == "President and CEO"


def test_event_key_is_acceptance_not_filing_date():
    """The event date must derive from acceptanceDateTime, never filingDate."""
    f = _filing(
        _form4([_txn()]),
        filing_date="2026-06-15",
        acceptance="2026-06-16 09:31:00",  # accepted next morning
    )
    r = parse_filing(f)[0]
    assert r.acceptance_datetime == "2026-06-16T09:31:00"
    assert r.event_date == "2026-06-16"     # from acceptance, NOT 2026-06-15
    assert r.filing_date == "2026-06-15"    # recorded but not the event key


def test_transaction_date_preserved():
    f = _filing(_form4([_txn(date_="2026-06-10")]))
    r = parse_filing(f)[0]
    assert r.transaction_date == "2026-06-10"


# ---- parse_filing: filtering -------------------------------------------


def test_drops_sales_and_non_purchase_codes():
    txns = [
        _txn(code="S", acq="D"),       # sale
        _txn(code="M", acq="A"),       # option exercise
        _txn(code="F", acq="D"),       # tax withholding
        _txn(code="G", acq="A"),       # gift
        _txn(code="P", acq="A"),       # the one keeper
    ]
    rows = parse_filing(_filing(_form4(txns)))
    assert len(rows) == 1
    assert rows[0].value > 0


def test_drops_disposed_purchase_code_edge():
    """A P code on the disposed side (rare/erroneous) is dropped."""
    rows = parse_filing(_filing(_form4([_txn(code="P", acq="D")])))
    assert rows == []


def test_zero_or_missing_shares_dropped():
    rows = parse_filing(_filing(_form4([_txn(shares=0), _txn(shares=None)])))
    assert rows == []


def test_multiple_purchases_in_one_filing():
    txns = [_txn(shares=100, price=5.0), _txn(shares=200, price=6.0)]
    rows = parse_filing(_filing(_form4(txns)))
    assert len(rows) == 2
    assert {r.shares for r in rows} == {100, 200}


# ---- parse_filing: 10b5-1 flag -----------------------------------------


def test_10b5_1_footnote_flagged():
    f = _filing(_form4([_txn(footnotes="Purchase under a Rule 10b5-1 trading plan.")]))
    r = parse_filing(f)[0]
    assert r.is_10b5_1 is True


def test_no_10b5_1_by_default():
    r = parse_filing(_filing(_form4([_txn(footnotes="Open market purchase.")])))[0]
    assert r.is_10b5_1 is False


# ---- parse_filing: robustness ------------------------------------------


def test_missing_acceptance_datetime_returns_empty():
    """No acceptance timestamp → cannot place on timeline → drop the filing."""
    f = SimpleNamespace(
        accession_no="x", filing_date="2026-06-15",
        header=SimpleNamespace(acceptance_datetime=None),
        obj=lambda: _form4([_txn()]),
    )
    assert parse_filing(f) == []


def test_obj_raises_returns_empty():
    def _boom():
        raise RuntimeError("xml parse boom")
    f = SimpleNamespace(
        accession_no="x", filing_date="2026-06-15",
        header=SimpleNamespace(acceptance_datetime=datetime(2026, 6, 15, 16, 0, 0)),
        obj=_boom,
    )
    assert parse_filing(f) == []


def test_no_transactions_returns_empty():
    assert parse_filing(_filing(_form4([]))) == []


def test_ten_pct_owner_roles_captured():
    owners = [_owner(director=False, officer=False, ten_pct=True, other=False,
                     officer_title=None, position="10% Owner")]
    r = parse_filing(_filing(_form4([_txn()], owners=owners)))[0]
    assert r.is_ten_pct_owner is True
    assert r.is_director is False
    assert r.is_officer is False


def test_none_ticker_preserved():
    r = parse_filing(_filing(_form4([_txn()], ticker=None)))[0]
    assert r.ticker is None


# ---- ingest_day + cache -------------------------------------------------


def _day_factory(filings):
    return lambda date_str: list(filings)


def test_ingest_day_aggregates(tmp_path):
    filings = [
        _filing(_form4([_txn(shares=100)]), accession="a"),
        _filing(_form4([_txn(code="S", acq="D")]), accession="b"),  # no purchase
        _filing(_form4([_txn(shares=300)]), accession="c"),
    ]
    rows = ingest_day("2020-01-02", cache_dir=tmp_path,
                      _filings_factory=_day_factory(filings))
    assert len(rows) == 2
    assert {r.shares for r in rows} == {100, 300}


def test_ingest_day_limit_not_cached(tmp_path):
    filings = [_filing(_form4([_txn(shares=100)]), accession=f"a{i}") for i in range(5)]
    rows = ingest_day("2020-01-02", cache_dir=tmp_path, limit=2,
                      _filings_factory=_day_factory(filings))
    assert len(rows) == 2
    # limited runs must NOT write cache (would masquerade as the full day)
    assert not (tmp_path / "2020-01-02.json").is_file()


def test_past_day_cache_served_without_factory(tmp_path):
    filings = [_filing(_form4([_txn(shares=100)]), accession="a")]
    ingest_day("2020-01-02", cache_dir=tmp_path, _filings_factory=_day_factory(filings))

    def _boom(date_str):
        raise AssertionError("factory should not be called on cache hit")

    rows = ingest_day("2020-01-02", cache_dir=tmp_path, _filings_factory=_boom)
    assert len(rows) == 1


def test_past_day_cache_never_expires(tmp_path):
    """A past day's cache is immutable — even a very old timestamp is honored."""
    filings = [_filing(_form4([_txn(shares=100)]), accession="a")]
    ingest_day("2020-01-02", cache_dir=tmp_path, _filings_factory=_day_factory(filings))
    p = tmp_path / "2020-01-02.json"
    data = json.loads(p.read_text())
    data["_cached_at_epoch"] = time.time() - 10_000_000  # ancient
    p.write_text(json.dumps(data))

    def _boom(date_str):
        raise AssertionError("past-day cache must not expire")

    rows = ingest_day("2020-01-02", cache_dir=tmp_path, _filings_factory=_boom)
    assert len(rows) == 1


def test_use_cache_false_bypasses(tmp_path):
    filings = [_filing(_form4([_txn(shares=100)]), accession="a")]
    ingest_day("2020-01-02", cache_dir=tmp_path, _filings_factory=_day_factory(filings))

    called = {"n": 0}

    def _f(date_str):
        called["n"] += 1
        return filings

    ingest_day("2020-01-02", cache_dir=tmp_path, use_cache=False, _filings_factory=_f)
    assert called["n"] == 1


def test_malformed_cache_falls_through(tmp_path):
    (tmp_path / "2020-01-02.json").write_text("not valid json{{{")
    filings = [_filing(_form4([_txn(shares=100)]), accession="a")]
    rows = ingest_day("2020-01-02", cache_dir=tmp_path,
                      _filings_factory=_day_factory(filings))
    assert len(rows) == 1


# ---- ingest_current -----------------------------------------------------


def test_ingest_current(tmp_path):
    filings = [
        _filing(_form4([_txn(shares=100)]), accession="a"),
        _filing(_form4([_txn(code="S", acq="D")]), accession="b"),
    ]
    rows = ingest_current(_filings_factory=lambda: list(filings))
    assert len(rows) == 1


# ---- compute / TraceEntry ----------------------------------------------


def test_compute_returns_trace_entry(tmp_path):
    filings = [_filing(_form4([_txn(shares=100)]), accession="a")]
    entry = compute("2020-01-02", cache_dir=tmp_path,
                    _filings_factory=_day_factory(filings))
    assert entry.tool == "tools/fundamentals/form4_insider_transactions.py"
    assert entry.inputs == {"date": "2020-01-02", "limit": None}
    assert entry.output["n_purchases"] == 1
    assert entry.output["purchases"][0]["shares"] == 100
    assert entry.fetched_at
