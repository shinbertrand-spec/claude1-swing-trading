"""Tests for tools.trace_rerun."""
from __future__ import annotations

import pytest

from tools.trace_rerun import (
    TraceRerunError,
    assert_traces_consistent,
    rerun,
)


def _ledger_with_steps(steps):
    return {"reasoning_trace": list(steps)}


def test_pure_tool_matches_when_inputs_replay_exactly():
    """Re-running compute_yoy(1.87, 1.55) reproduces recorded output."""
    from tools.compute_yoy import compute as yoy_compute
    recorded = yoy_compute(1.87, 1.55)
    recorded_dict = {
        "id": 1,
        "tool": recorded.tool,
        "inputs": recorded.inputs,
        "output": recorded.output,
        "fetched_at": recorded.fetched_at,
    }
    report = rerun(_ledger_with_steps([recorded_dict]))
    assert len(report.results) == 1
    assert report.results[0].status == "match"
    assert report.results[0].klass == "pure"


def test_pure_tool_divergent_detected():
    """Mutate recorded output → divergent."""
    from tools.compute_yoy import compute as yoy_compute
    recorded = yoy_compute(1.87, 1.55)
    bad_output = dict(recorded.output)
    bad_output["yoy_growth_decimal"] = 0.99   # wrong
    recorded_dict = {
        "id": 1,
        "tool": recorded.tool,
        "inputs": recorded.inputs,
        "output": bad_output,
        "fetched_at": recorded.fetched_at,
    }
    report = rerun(_ledger_with_steps([recorded_dict]))
    r = report.results[0]
    assert r.status == "divergent"
    assert r.diffs[0]["path"].endswith("yoy_growth_decimal")


def test_pure_tool_signature_mismatch_detected():
    """Recorded inputs don't match compute() signature → divergent."""
    recorded_dict = {
        "id": 1,
        "tool": "tools/compute_yoy.py",
        "inputs": {"foo": "bar"},   # wrong shape
        "output": {"yoy_growth_decimal": 0.0},
        "fetched_at": "2026-05-17T14:30:00Z",
    }
    report = rerun(_ledger_with_steps([recorded_dict]))
    r = report.results[0]
    assert r.status == "divergent"
    assert "signature" in r.detail or "argument" in r.detail or "unexpected" in r.detail.lower()


def test_ohlcv_tool_shape_ok():
    """OHLCV tool with correct keys → shape_ok."""
    recorded = {
        "id": 1,
        "tool": "tools/atr_compute.py",
        "inputs": {"period": 14, "ticker": "AAPL"},
        "output": {
            "atr": 4.5,
            "atr_pct_of_close": 0.023,
            "period": 14,
            "last_close": 192.74,
            "last_close_date": "2026-05-17",
        },
        "fetched_at": "2026-05-17T14:30:00Z",
    }
    report = rerun(_ledger_with_steps([recorded]))
    r = report.results[0]
    assert r.status == "shape_ok"
    assert r.klass == "ohlcv"


def test_ohlcv_tool_shape_fail_missing_key():
    recorded = {
        "id": 1,
        "tool": "tools/atr_compute.py",
        "inputs": {"period": 14},
        "output": {"atr": 4.5},   # missing required keys
        "fetched_at": "2026-05-17T14:30:00Z",
    }
    report = rerun(_ledger_with_steps([recorded]))
    r = report.results[0]
    assert r.status == "shape_fail"


def test_manual_tool_well_formed():
    recorded = {
        "id": 1,
        "tool": "manual:broker_api",
        "inputs": {"ticker": "AAPL"},
        "output": {"some": "value"},
        "fetched_at": "2026-05-17T14:30:00Z",
    }
    report = rerun(_ledger_with_steps([recorded]))
    r = report.results[0]
    assert r.status == "well_formed"
    assert r.klass == "manual"


def test_unknown_tool_flagged():
    recorded = {
        "id": 1,
        "tool": "tools/nonexistent.py",
        "inputs": {},
        "output": None,
        "fetched_at": "2026-05-17T14:30:00Z",
    }
    report = rerun(_ledger_with_steps([recorded]))
    r = report.results[0]
    assert r.status == "unknown_tool"


def test_assert_raises_on_divergence():
    recorded = {
        "id": 1,
        "tool": "tools/compute_yoy.py",
        "inputs": {"current": 2.0, "prior": 1.0},
        "output": {"yoy_growth_decimal": 99.0, "yoy_growth_pct": 9900.0},   # wrong
        "fetched_at": "2026-05-17T14:30:00Z",
    }
    with pytest.raises(TraceRerunError, match="step id=1"):
        assert_traces_consistent(_ledger_with_steps([recorded]))


def test_assert_passes_on_clean():
    from tools.stop_sizer import compute as stop_compute
    recorded = stop_compute(entry_price=192.74, atr=4.57)
    step = {
        "id": 1,
        "tool": recorded.tool,
        "inputs": recorded.inputs,
        "output": recorded.output,
        "fetched_at": recorded.fetched_at,
    }
    entry = assert_traces_consistent(_ledger_with_steps([step]))
    assert entry.output["has_divergence"] is False
