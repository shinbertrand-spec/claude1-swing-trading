"""Tests for tools.auto_paper.shell_ledger — Phase 2 builder."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from tools.auto_paper.shell_ledger import (
    ShellLedgerInput,
    build_quant_shell_ledger,
    write_bull_stub_report,
    write_shell_ledger,
)
from tools.contract import TraceEntry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schema():
    schema_path = Path(__file__).resolve().parents[1] / "ledgers" / "_schema" / "ledger.schema.json"
    with open(schema_path, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def validator(schema):
    return Draft202012Validator(schema)


def _vrt_input(**overrides) -> ShellLedgerInput:
    base = dict(
        ticker="VRT",
        setup_type="xs_short_term_reversal",
        setup_grade="B",
        pivot_price=328.10,
        stop_price=289.66,
        sector_etf="XLI",
    )
    base.update(overrides)
    return ShellLedgerInput(**base)


def _clean_screener_output(*, next_earnings_date="2026-07-29", trading_days=45) -> dict:
    return {
        "blocked": False,
        "corrected_sector_etf": None,
        "checks": [
            {
                "check": "earnings_blackout", "passed": True,
                "evidence": {
                    "next_earnings_date": next_earnings_date,
                    "trading_days_to_earnings": trading_days,
                },
            },
            {"check": "litigation", "passed": True, "evidence": {"headlines_scanned": 80}},
            {"check": "dilution", "passed": True, "evidence": {"headlines_scanned": 80}},
            {"check": "sector_lookup", "passed": True, "evidence": {"mismatch": False}},
        ],
    }


# ---------------------------------------------------------------------------
# build_quant_shell_ledger
# ---------------------------------------------------------------------------


def test_minimal_ledger_validates(validator):
    """No screener, no tool runs — minimum-viable ledger still schema-valid."""
    inp = _vrt_input()
    ledger, _ = build_quant_shell_ledger(inp, today=date(2026, 5, 27), run_tools=False)
    errors = sorted(validator.iter_errors(ledger), key=lambda e: list(e.path))
    assert errors == [], (
        f"Schema errors: {[(list(e.path), e.message) for e in errors]}"
    )
    assert ledger["meta"]["state"] == "candidate"
    assert ledger["meta"]["account_track"] == "paper-auto"
    assert ledger["meta"]["created_by"] == "auto_paper/shell_ledger"
    assert ledger["setup_classification"]["type"] == "xs_short_term_reversal"
    assert ledger["setup_classification"]["grade"] == "B"
    assert ledger["setup_classification"]["pivot_price"] == 328.10
    assert ledger["setup_classification"]["stop_price"] == 289.66
    # stop_distance_pct = (328.10 - 289.66) / 328.10 ≈ 0.1172
    assert abs(ledger["setup_classification"]["stop_distance_pct"] - 0.1172) < 0.001


def test_screener_evidence_threads_to_fundamentals(validator):
    """When screener output carries earnings_blackout evidence, the shell
    ledger populates the fundamentals.next_earnings_date field."""
    inp = _vrt_input(screener_output=_clean_screener_output(
        next_earnings_date="2026-07-15", trading_days=33,
    ))
    ledger, _ = build_quant_shell_ledger(inp, today=date(2026, 5, 27), run_tools=False)
    assert "fundamentals" in ledger
    assert ledger["fundamentals"]["next_earnings_date"] == "2026-07-15"
    assert ledger["fundamentals"]["source"] == "tool:auto_paper/screener.py"
    errors = list(validator.iter_errors(ledger))
    assert errors == []


def test_screener_with_block_adds_no_clean_confluence_row():
    """If screener BLOCKED, the 'strategy-blind disqualifiers cleared' row
    is not added — that would be misleading."""
    inp = _vrt_input(screener_output={
        "blocked": True,
        "blocking_checks": ["litigation"],
        "checks": [{"check": "litigation", "passed": False, "evidence": {}}],
    })
    ledger, _ = build_quant_shell_ledger(inp, today=date(2026, 5, 27), run_tools=False)
    criteria = [
        c["criterion"] for c in ledger["setup_classification"]["confluence_checklist"]
    ]
    assert not any("disqualifiers cleared" in c for c in criteria)


def test_seed_trace_renumbered_starting_at_1():
    """Caller-supplied seed traces get sequential ids 1..N, regardless of
    any pre-set id values."""
    inp = _vrt_input()
    seeds = [
        TraceEntry(tool="tools/seed_a.py", inputs={}, output={"x": 1}),
        TraceEntry(tool="tools/seed_b.py", inputs={}, output={"x": 2}, id=999),
    ]
    inp.seed_trace = seeds
    ledger, traces = build_quant_shell_ledger(
        inp, today=date(2026, 5, 27), run_tools=False,
    )
    ids = [t.id for t in traces]
    assert ids == [1, 2]
    assert ledger["reasoning_trace"][0]["id"] == 1
    assert ledger["reasoning_trace"][1]["id"] == 2


def test_tool_failures_recorded_in_meta(validator, monkeypatch):
    """If regime_check or atr_compute raise, the ledger still validates and
    the failures are recorded in meta.updated_by."""
    def _fail_regime(*a, **kw):
        raise RuntimeError("yfinance unreachable")
    def _fail_atr(*a, **kw):
        raise RuntimeError("network error")
    from tools.auto_paper import shell_ledger as _sl
    monkeypatch.setattr(_sl, "_run_regime", lambda t, s: (None, "yfinance unreachable"))
    monkeypatch.setattr(_sl, "_run_atr", lambda t: (None, "network error"))

    inp = _vrt_input()
    ledger, _ = build_quant_shell_ledger(inp, today=date(2026, 5, 27), run_tools=True)
    assert "yfinance unreachable" in ledger["meta"].get("updated_by", "")
    assert "network error" in ledger["meta"].get("updated_by", "")
    errors = list(validator.iter_errors(ledger))
    assert errors == []


def test_live_tool_path_validates_against_schema(validator, monkeypatch):
    """Mocked regime_check + atr_compute populate regime + technical blocks."""
    from tools.auto_paper import shell_ledger as _sl

    fake_regime = TraceEntry(
        tool="tools/regime_check.py",
        inputs={"candidate_ticker": "VRT", "sector_etf": "XLI"},
        output={
            "broad_market": {
                "ticker": "SPY", "trend_template_passes": 7,
                "stage_class": "stage_2_confirmed", "stage": 2,
            },
            "sector": {
                "ticker": "XLI", "trend_template_passes": 6,
                "stage_class": "stage_2_weakening",
                "qualifies_for_long": True, "stage": 2,
            },
            "candidate": {
                "ticker": "VRT", "trend_template_passes": 7,
                "stage": 2, "criteria": {},
            },
            "regime_multiplier": 1.0,
            "candidate_qualifies_for_entry": True,
            "circuit_breaker_stage_4": False,
        },
    )
    fake_atr = TraceEntry(
        tool="tools/atr_compute.py",
        inputs={"ticker": "VRT", "period": 14},
        output={"atr_14": 18.12},
    )
    monkeypatch.setattr(_sl, "_run_regime", lambda t, s: (fake_regime, None))
    monkeypatch.setattr(_sl, "_run_atr", lambda t: (fake_atr, None))

    inp = _vrt_input(screener_output=_clean_screener_output())
    ledger, traces = build_quant_shell_ledger(
        inp, today=date(2026, 5, 27), run_tools=True,
    )
    errors = sorted(validator.iter_errors(ledger), key=lambda e: list(e.path))
    assert errors == [], (
        f"Schema errors: {[(list(e.path), e.message) for e in errors]}"
    )
    assert ledger["regime"]["sector_etf"] == "XLI"
    assert ledger["regime"]["sector_qualifies_for_long"] is True
    assert ledger["technical"]["atr_14"] == 18.12
    assert ledger["technical"]["trend_template_passes"] == 7
    assert ledger["technical"]["stage"] == 2
    # Both fake tool entries land in reasoning_trace with ids 1 and 2
    assert len(ledger["reasoning_trace"]) == 2
    assert ledger["reasoning_trace"][0]["id"] == 1
    assert ledger["reasoning_trace"][1]["id"] == 2


# ---------------------------------------------------------------------------
# write_shell_ledger
# ---------------------------------------------------------------------------


def test_write_shell_ledger_writes_yaml(tmp_path):
    inp = _vrt_input()
    ledger, _ = build_quant_shell_ledger(inp, today=date(2026, 5, 27), run_tools=False)
    path = write_shell_ledger(
        ledger, ticker="VRT", ledger_date=date(2026, 5, 27),
        candidates_dir=tmp_path,
    )
    assert path.exists()
    with open(path, encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    assert loaded["meta"]["ticker"] == "VRT"
    assert loaded["setup_classification"]["pivot_price"] == 328.10


def test_write_shell_ledger_refuses_overwrite(tmp_path):
    """An existing ledger (likely a discretionary deep-dive) is preserved."""
    inp = _vrt_input()
    ledger, _ = build_quant_shell_ledger(inp, today=date(2026, 5, 27), run_tools=False)
    path = write_shell_ledger(
        ledger, ticker="VRT", ledger_date=date(2026, 5, 27),
        candidates_dir=tmp_path,
    )
    with pytest.raises(FileExistsError):
        write_shell_ledger(
            ledger, ticker="VRT", ledger_date=date(2026, 5, 27),
            candidates_dir=tmp_path,
        )
    # Force-overwrite path works
    path2 = write_shell_ledger(
        ledger, ticker="VRT", ledger_date=date(2026, 5, 27),
        candidates_dir=tmp_path, overwrite=True,
    )
    assert path2 == path


# ---------------------------------------------------------------------------
# write_bull_stub_report
# ---------------------------------------------------------------------------


def test_write_bull_stub_report(tmp_path):
    inp = _vrt_input()
    ledger, _ = build_quant_shell_ledger(inp, today=date(2026, 5, 27), run_tools=False)
    ledger_path = write_shell_ledger(
        ledger, ticker="VRT", ledger_date=date(2026, 5, 27),
        candidates_dir=tmp_path,
    )
    md_path = write_bull_stub_report(
        inp=inp, ledger_path=ledger_path,
        ledger_date=date(2026, 5, 27), candidates_dir=tmp_path,
    )
    assert md_path.exists()
    content = md_path.read_text(encoding="utf-8")
    assert "Quant signal" not in content or "quant track" in content
    assert "xs_short_term_reversal" in content
    assert "$328.10" in content
    assert "$289.66" in content
    assert "11.72%" in content  # stop distance


def test_write_bull_stub_does_not_clobber_existing(tmp_path):
    """If a real bull report exists from trade-researcher, don't overwrite."""
    inp = _vrt_input()
    ledger, _ = build_quant_shell_ledger(inp, today=date(2026, 5, 27), run_tools=False)
    ledger_path = write_shell_ledger(
        ledger, ticker="VRT", ledger_date=date(2026, 5, 27),
        candidates_dir=tmp_path,
    )
    # Simulate an existing discretionary bull report
    md_path_real = tmp_path / "2026-05-27" / "VRT.md"
    md_path_real.write_text("REAL BULL REPORT — DO NOT OVERWRITE", encoding="utf-8")
    md_path = write_bull_stub_report(
        inp=inp, ledger_path=ledger_path,
        ledger_date=date(2026, 5, 27), candidates_dir=tmp_path,
    )
    assert md_path == md_path_real
    # Content unchanged
    assert md_path.read_text(encoding="utf-8") == "REAL BULL REPORT — DO NOT OVERWRITE"
