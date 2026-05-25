"""Tests for tools.thematic_portfolio.corpus.thirteen_f.

The edgartools library is mocked at the module level so tests don't hit SEC EDGAR.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from tools.thematic_portfolio.corpus import (
    ENSEMBLE_CIKS,
    SA_LP_CIK_PRIMARY,
)
from tools.thematic_portfolio.corpus import thirteen_f as t13f


# ---------------------------------------------------------------------------
# Fake edgartools — installed via monkeypatch.setattr in each test
# ---------------------------------------------------------------------------


class _FakeFilingObj:
    def __init__(self, df: pd.DataFrame):
        self.infotable = df


class _FakeFiling:
    def __init__(self, form: str, period: str, filing_date: str, df: pd.DataFrame):
        self.form = form
        self.period_of_report = period
        self.filing_date = filing_date
        self._df = df

    def obj(self) -> _FakeFilingObj:
        return _FakeFilingObj(self._df)


class _FakeEntity:
    def __init__(self, filings: list[_FakeFiling]):
        self._filings = filings

    def get_filings(self) -> list[_FakeFiling]:
        return self._filings


class _FakeEdgar:
    """Drop-in replacement for the ``edgar`` module in tests."""

    def __init__(self):
        self.identity_set_to: str | None = None
        # Per-CIK fake-filings registry, populated by test setup.
        self._by_cik: dict[str, list[_FakeFiling]] = {}

    def set_identity(self, identity: str) -> None:
        self.identity_set_to = identity

    def Entity(self, cik: str) -> _FakeEntity:  # noqa: N802 — mirrors edgartools API
        return _FakeEntity(self._by_cik.get(cik, []))


def _make_infotable_df(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Build a realistic infotable DataFrame; pads missing columns to None."""
    cols = ["Issuer", "Cusip", "Value", "Shares", "Ticker", "PutCall", "Type"]
    normalized = [{c: r.get(c) for c in cols} for r in rows]
    return pd.DataFrame(normalized, columns=cols)


@pytest.fixture
def fake_edgar(monkeypatch: pytest.MonkeyPatch) -> _FakeEdgar:
    """Install a fake edgartools module into the import cache + module ref."""
    fake = _FakeEdgar()
    import sys
    monkeypatch.setitem(sys.modules, "edgar", fake)
    return fake


# ---------------------------------------------------------------------------
# fetch_and_normalize
# ---------------------------------------------------------------------------


def test_fetch_normalizes_long_book_only(fake_edgar: _FakeEdgar, tmp_path: Path):
    """SA LP Q1 2026-style filing with 3 long positions, no puts/calls."""
    df = _make_infotable_df([
        {"Issuer": "BLOOM ENERGY CORP", "Cusip": "093712107", "Value": 877300000.0,
         "Shares": 50000000, "Ticker": "BE", "PutCall": None, "Type": "SH"},
        {"Issuer": "SANDISK CORP", "Cusip": "80004C101", "Value": 725900000.0,
         "Shares": 12000000, "Ticker": "SNDK", "PutCall": None, "Type": "SH"},
        {"Issuer": "COREWEAVE INC", "Cusip": "21873L106", "Value": 554500000.0,
         "Shares": 5800000, "Ticker": "CRWV", "PutCall": None, "Type": "SH"},
    ])
    fake_edgar._by_cik[SA_LP_CIK_PRIMARY] = [
        _FakeFiling("13F-HR", "2026-03-31", "2026-05-18", df),
    ]
    result = t13f.fetch_and_normalize(
        cik=SA_LP_CIK_PRIMARY,
        period="2026-03-31",
        out_dir=tmp_path,
        fund_label="sa_lp",
    )
    assert result["counts"] == {"long": 3, "puts": 0, "calls": 0}
    assert result["filed_date"] == "2026-05-18"

    long_path = Path(result["files"]["long"])
    long_data = json.loads(long_path.read_text())
    assert len(long_data) == 3
    assert long_data[0]["ticker"] == "BE"
    assert long_data[0]["value_usd"] == 877300000.0
    assert long_data[0]["cusip"] == "093712107"
    # put_call key is stripped from output rows (file name encodes the leg).
    assert "put_call" not in long_data[0]


def test_fetch_splits_puts_calls_and_longs(fake_edgar: _FakeEdgar, tmp_path: Path):
    df = _make_infotable_df([
        {"Issuer": "BLOOM ENERGY", "Cusip": "X1", "Value": 100.0, "Ticker": "BE", "PutCall": None},
        {"Issuer": "NVIDIA CORP", "Cusip": "X2", "Value": 200.0, "Ticker": "NVDA", "PutCall": "Put"},
        {"Issuer": "MICRON TECH", "Cusip": "X3", "Value": 300.0, "Ticker": "MU", "PutCall": "Put"},
        {"Issuer": "AMD", "Cusip": "X4", "Value": 50.0, "Ticker": "AMD", "PutCall": "Call"},
    ])
    fake_edgar._by_cik["0001234567"] = [
        _FakeFiling("13F-HR", "2026-03-31", "2026-05-15", df),
    ]
    result = t13f.fetch_and_normalize(
        cik="0001234567",
        period="2026-03-31",
        out_dir=tmp_path,
    )
    assert result["counts"] == {"long": 1, "puts": 2, "calls": 1}
    puts_data = json.loads(Path(result["files"]["puts"]).read_text())
    assert {p["ticker"] for p in puts_data} == {"NVDA", "MU"}
    calls_data = json.loads(Path(result["files"]["calls"]).read_text())
    assert calls_data[0]["ticker"] == "AMD"


def test_fetch_raises_on_period_mismatch(fake_edgar: _FakeEdgar, tmp_path: Path):
    df = _make_infotable_df([
        {"Issuer": "X", "Cusip": "X", "Value": 100.0, "Ticker": "X"},
    ])
    fake_edgar._by_cik["0001234567"] = [
        _FakeFiling("13F-HR", "2026-03-31", "2026-05-15", df),
    ]
    with pytest.raises(ValueError, match="no 13F-HR found"):
        t13f.fetch_and_normalize(
            cik="0001234567",
            period="2025-12-31",  # different period — should miss
            out_dir=tmp_path,
        )


def test_fetch_raises_on_empty_infotable(fake_edgar: _FakeEdgar, tmp_path: Path):
    df = _make_infotable_df([])
    fake_edgar._by_cik["0001234567"] = [
        _FakeFiling("13F-HR", "2026-03-31", "2026-05-15", df),
    ]
    with pytest.raises(ValueError, match="empty infotable"):
        t13f.fetch_and_normalize(
            cik="0001234567", period="2026-03-31", out_dir=tmp_path
        )


def test_fetch_skips_non_13fhr_forms(fake_edgar: _FakeEdgar, tmp_path: Path):
    """A 10-K with the same period_of_report should NOT be picked up."""
    df_10k = _make_infotable_df([
        {"Issuer": "X", "Cusip": "X", "Value": 100.0, "Ticker": "X"},
    ])
    df_13f = _make_infotable_df([
        {"Issuer": "BE", "Cusip": "BE", "Value": 877.0, "Ticker": "BE"},
    ])
    fake_edgar._by_cik["0001234567"] = [
        _FakeFiling("10-K", "2026-03-31", "2026-05-15", df_10k),
        _FakeFiling("13F-HR", "2026-03-31", "2026-05-18", df_13f),
    ]
    result = t13f.fetch_and_normalize(
        cik="0001234567", period="2026-03-31", out_dir=tmp_path
    )
    assert result["filed_date"] == "2026-05-18"  # picked the 13F-HR, not the 10-K


def test_identity_override_via_param(fake_edgar: _FakeEdgar, tmp_path: Path):
    df = _make_infotable_df([
        {"Issuer": "X", "Cusip": "X", "Value": 100.0, "Ticker": "X"},
    ])
    fake_edgar._by_cik["0001234567"] = [
        _FakeFiling("13F-HR", "2026-03-31", "2026-05-15", df),
    ]
    t13f.fetch_and_normalize(
        cik="0001234567",
        period="2026-03-31",
        out_dir=tmp_path,
        identity="Alice Tester alice@test.com",
    )
    assert fake_edgar.identity_set_to == "Alice Tester alice@test.com"


def test_identity_env_var_used_when_no_override(
    fake_edgar: _FakeEdgar, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("EDGAR_IDENTITY", "Env User env@test.com")
    df = _make_infotable_df([
        {"Issuer": "X", "Cusip": "X", "Value": 100.0, "Ticker": "X"},
    ])
    fake_edgar._by_cik["0001234567"] = [
        _FakeFiling("13F-HR", "2026-03-31", "2026-05-15", df),
    ]
    t13f.fetch_and_normalize(
        cik="0001234567", period="2026-03-31", out_dir=tmp_path
    )
    assert fake_edgar.identity_set_to == "Env User env@test.com"


def test_output_long_book_loadable_by_sizer(fake_edgar: _FakeEdgar, tmp_path: Path):
    """Regression: the long-book JSON must be directly loadable by the sizer."""
    from tools.thematic_portfolio.sizer import load_long_book_from_json

    df = _make_infotable_df([
        {"Issuer": "BLOOM ENERGY CORP", "Cusip": "093712107", "Value": 877300000.0,
         "Ticker": "BE", "PutCall": None},
        {"Issuer": "SANDISK CORP", "Cusip": "80004C101", "Value": 725900000.0,
         "Ticker": "SNDK", "PutCall": None},
    ])
    fake_edgar._by_cik[SA_LP_CIK_PRIMARY] = [
        _FakeFiling("13F-HR", "2026-03-31", "2026-05-18", df),
    ]
    result = t13f.fetch_and_normalize(
        cik=SA_LP_CIK_PRIMARY, period="2026-03-31", out_dir=tmp_path,
    )
    book = load_long_book_from_json(Path(result["files"]["long"]))
    assert [p.ticker for p in book] == ["BE", "SNDK"]
    assert book[0].value_usd == 877300000.0
    assert book[0].cusip == "093712107"


# ---------------------------------------------------------------------------
# fetch_one (TraceEntry wrapper)
# ---------------------------------------------------------------------------


def test_fetch_one_returns_trace_entry(fake_edgar: _FakeEdgar, tmp_path: Path):
    df = _make_infotable_df([
        {"Issuer": "BE", "Cusip": "BE", "Value": 877.0, "Ticker": "BE"},
    ])
    fake_edgar._by_cik[SA_LP_CIK_PRIMARY] = [
        _FakeFiling("13F-HR", "2026-03-31", "2026-05-18", df),
    ]
    entry = t13f.fetch_one(
        cik=SA_LP_CIK_PRIMARY,
        period="2026-03-31",
        out_dir=tmp_path,
        fund_label="sa_lp",
    )
    assert entry.tool == t13f.TOOL
    assert entry.inputs["cik"] == SA_LP_CIK_PRIMARY
    assert entry.inputs["fund_label"] == "sa_lp"
    assert entry.output["counts"]["long"] == 1


# ---------------------------------------------------------------------------
# fetch_ensemble
# ---------------------------------------------------------------------------


def test_fetch_ensemble_pulls_all_funds(fake_edgar: _FakeEdgar, tmp_path: Path):
    """SA LP + all 3 ensemble funds at the same period; per-fund subdirs."""
    df = _make_infotable_df([
        {"Issuer": "X", "Cusip": "X", "Value": 100.0, "Ticker": "X"},
    ])
    fake_edgar._by_cik[SA_LP_CIK_PRIMARY] = [
        _FakeFiling("13F-HR", "2026-03-31", "2026-05-18", df),
    ]
    for cik in ENSEMBLE_CIKS.values():
        fake_edgar._by_cik[cik] = [
            _FakeFiling("13F-HR", "2026-03-31", "2026-05-15", df),
        ]
    entry = t13f.fetch_ensemble(period="2026-03-31", out_dir_root=tmp_path)
    out = entry.output
    assert out["n_succeeded"] == 1 + len(ENSEMBLE_CIKS)  # SA LP + 3 ensemble
    assert out["n_failed"] == 0
    assert "sa_lp" in out["per_fund"]
    for label in ENSEMBLE_CIKS:
        assert label in out["per_fund"]
    # Each fund's files land in its own subdirectory.
    assert (tmp_path / "sa_lp" / f"{SA_LP_CIK_PRIMARY}-2026-03-31-long.json").exists()
    for label, cik in ENSEMBLE_CIKS.items():
        assert (tmp_path / label / f"{cik}-2026-03-31-long.json").exists()


def test_fetch_ensemble_handles_missing_fund_gracefully(
    fake_edgar: _FakeEdgar, tmp_path: Path
):
    """Light Street Photon-style scenario: one fund has no 13F at this period."""
    df = _make_infotable_df([
        {"Issuer": "X", "Cusip": "X", "Value": 100.0, "Ticker": "X"},
    ])
    fake_edgar._by_cik[SA_LP_CIK_PRIMARY] = [
        _FakeFiling("13F-HR", "2026-03-31", "2026-05-18", df),
    ]
    fake_edgar._by_cik[ENSEMBLE_CIKS["altimeter"]] = [
        _FakeFiling("13F-HR", "2026-03-31", "2026-05-15", df),
    ]
    fake_edgar._by_cik[ENSEMBLE_CIKS["coatue"]] = [
        _FakeFiling("13F-HR", "2026-03-31", "2026-05-15", df),
    ]
    # light_street: deliberately empty — has no 13F at this period
    fake_edgar._by_cik[ENSEMBLE_CIKS["light_street"]] = []

    entry = t13f.fetch_ensemble(period="2026-03-31", out_dir_root=tmp_path)
    out = entry.output
    assert out["n_succeeded"] == 3  # sa_lp + altimeter + coatue
    assert out["n_failed"] == 1
    assert "light_street" in out["errors"]
    assert "no 13F-HR" in out["errors"]["light_street"]


# ---------------------------------------------------------------------------
# Edge cases / robustness
# ---------------------------------------------------------------------------


def test_unknown_put_call_value_buckets_to_long(fake_edgar: _FakeEdgar, tmp_path: Path):
    """Edgartools occasionally yields odd PutCall values — handle gracefully."""
    df = _make_infotable_df([
        {"Issuer": "X", "Cusip": "X1", "Value": 100.0, "Ticker": "X", "PutCall": "Unknown"},
        {"Issuer": "Y", "Cusip": "X2", "Value": 200.0, "Ticker": "Y", "PutCall": None},
    ])
    fake_edgar._by_cik["0001234567"] = [
        _FakeFiling("13F-HR", "2026-03-31", "2026-05-15", df),
    ]
    result = t13f.fetch_and_normalize(
        cik="0001234567", period="2026-03-31", out_dir=tmp_path,
    )
    # Both rows land in long; neither puts nor calls.
    assert result["counts"] == {"long": 2, "puts": 0, "calls": 0}


def test_total_long_book_value_aggregates(fake_edgar: _FakeEdgar, tmp_path: Path):
    df = _make_infotable_df([
        {"Issuer": "A", "Cusip": "A", "Value": 100.0, "Ticker": "A"},
        {"Issuer": "B", "Cusip": "B", "Value": 200.0, "Ticker": "B"},
        {"Issuer": "PUT", "Cusip": "P", "Value": 9999.0, "Ticker": "P", "PutCall": "Put"},
    ])
    fake_edgar._by_cik["0001234567"] = [
        _FakeFiling("13F-HR", "2026-03-31", "2026-05-15", df),
    ]
    result = t13f.fetch_and_normalize(
        cik="0001234567", period="2026-03-31", out_dir=tmp_path,
    )
    # Only longs aggregate; puts excluded.
    assert result["total_long_book_value_usd"] == 300.0
