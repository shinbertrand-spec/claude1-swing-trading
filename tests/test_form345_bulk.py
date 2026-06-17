"""Tests for tools.fundamentals.form345_bulk — bulk Form 345 loader.

Builds synthetic bulk zips in tmp_path (no network) and asserts the join +
filtering + field mapping. Date-range quarter math tested directly.
"""
from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path

import pytest

from tools.fundamentals.form345_bulk import (
    _parse_sec_date,
    _quarters_in_range,
    _roles,
    parse_zip,
)

SUBMISSION_COLS = "ACCESSION_NUMBER\tFILING_DATE\tDOCUMENT_TYPE\tISSUERCIK\tISSUERNAME\tISSUERTRADINGSYMBOL\tAFF10B5ONE"
OWNER_COLS = "ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP\tRPTOWNER_TITLE"
TRANS_COLS = ("ACCESSION_NUMBER\tTRANS_CODE\tTRANS_DATE\tTRANS_SHARES\tTRANS_PRICEPERSHARE"
              "\tTRANS_ACQUIRED_DISP_CD\tSHRS_OWND_FOLWNG_TRANS\tDIRECT_INDIRECT_OWNERSHIP")


def _make_zip(tmp_path, submissions, owners, transes) -> Path:
    p = tmp_path / "2024q1_form345.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("SUBMISSION.tsv", SUBMISSION_COLS + "\n" + "\n".join(submissions))
        z.writestr("REPORTINGOWNER.tsv", OWNER_COLS + "\n" + "\n".join(owners))
        z.writestr("NONDERIV_TRANS.tsv", TRANS_COLS + "\n" + "\n".join(transes))
    return p


def test_parse_zip_basic(tmp_path):
    subs = ["ACC1\t15-MAR-2024\t4\t0000123\tACME CORP\tACME\t0"]
    owns = ["ACC1\t0000999\tJane Insider\tDirector,Officer\tCEO"]
    trans = ["ACC1\tP\t12-MAR-2024\t1000.0\t10.50\tA\t5000.0\tD"]
    rows = parse_zip(_make_zip(tmp_path, subs, owns, trans))
    assert len(rows) == 1
    r = rows[0]
    assert r.ticker == "ACME"
    assert r.insider_name == "Jane Insider"
    assert r.is_director and r.is_officer and not r.is_ten_pct_owner
    assert r.shares == 1000.0
    assert r.price == pytest.approx(10.50)
    assert r.value == pytest.approx(10500.0)
    assert r.event_date == "2024-03-15"        # from FILING_DATE
    assert r.transaction_date == "2024-03-12"  # from TRANS_DATE
    assert r.is_10b5_1 is False
    assert r.source.startswith("sec-bulk")


def test_parse_zip_filters_non_purchase(tmp_path):
    subs = ["A1\t15-MAR-2024\t4\t1\tX\tXX\t0", "A2\t15-MAR-2024\t4\t1\tY\tYY\t0"]
    owns = ["A1\t9\tA\tOfficer\tCEO", "A2\t9\tB\tOfficer\tCEO"]
    trans = [
        "A1\tP\t12-MAR-2024\t100\t5\tA\t1\tD",     # purchase, keep
        "A2\tS\t12-MAR-2024\t100\t5\tD\t1\tD",     # sale, drop
    ]
    rows = parse_zip(_make_zip(tmp_path, subs, owns, trans))
    assert [r.ticker for r in rows] == ["XX"]


def test_parse_zip_excludes_amendments_by_default(tmp_path):
    subs = ["A1\t15-MAR-2024\t4/A\t1\tX\tXX\t0"]
    owns = ["A1\t9\tA\tOfficer\tCEO"]
    trans = ["A1\tP\t12-MAR-2024\t100\t5\tA\t1\tD"]
    assert parse_zip(_make_zip(tmp_path, subs, owns, trans)) == []
    # but included when explicitly requested
    rows = parse_zip(_make_zip(tmp_path, subs, owns, trans), forms=("4", "4/A"))
    assert len(rows) == 1


def test_parse_zip_10b5_1_flag(tmp_path):
    subs = ["A1\t15-MAR-2024\t4\t1\tX\tXX\t1"]   # AFF10B5ONE = 1
    owns = ["A1\t9\tA\tOfficer\tCEO"]
    trans = ["A1\tP\t12-MAR-2024\t100\t5\tA\t1\tD"]
    rows = parse_zip(_make_zip(tmp_path, subs, owns, trans))
    assert rows[0].is_10b5_1 is True


def test_parse_zip_ten_pct_only_role(tmp_path):
    subs = ["A1\t15-MAR-2024\t4\t1\tBANK\tNONE\t0"]   # NONE ticker → None
    owns = ["A1\t9\tBANK OF AMERICA\tTenPercentOwner\t"]
    trans = ["A1\tP\t12-MAR-2024\t1\t0.02\tA\t1\tI"]
    rows = parse_zip(_make_zip(tmp_path, subs, owns, trans))
    assert rows[0].ticker is None
    assert rows[0].is_ten_pct_owner is True
    assert not rows[0].is_officer and not rows[0].is_director


def test_parse_zip_zero_shares_dropped(tmp_path):
    subs = ["A1\t15-MAR-2024\t4\t1\tX\tXX\t0"]
    owns = ["A1\t9\tA\tOfficer\tCEO"]
    trans = ["A1\tP\t12-MAR-2024\t0\t5\tA\t1\tD"]
    assert parse_zip(_make_zip(tmp_path, subs, owns, trans)) == []


# ---- helpers ------------------------------------------------------------


def test_parse_sec_date():
    assert _parse_sec_date("28-FEB-2024") == "2024-02-28"
    assert _parse_sec_date("31-JAN-2024") == "2024-01-31"
    assert _parse_sec_date("2024-03-15") == "2024-03-15"
    assert _parse_sec_date("") is None
    assert _parse_sec_date("garbage") is None


def test_roles_parsing():
    assert _roles("Director,Officer") == {
        "is_director": True, "is_officer": True,
        "is_ten_pct_owner": False, "is_other": False}
    assert _roles("TenPercentOwner")["is_ten_pct_owner"] is True
    assert _roles("")["is_officer"] is False


def test_quarters_in_range():
    assert _quarters_in_range(date(2024, 2, 1), date(2024, 8, 1)) == [
        (2024, 1), (2024, 2), (2024, 3)]
    assert _quarters_in_range(date(2023, 11, 1), date(2024, 2, 1)) == [
        (2023, 4), (2024, 1)]
    assert _quarters_in_range(date(2024, 1, 1), date(2024, 1, 1)) == [(2024, 1)]
