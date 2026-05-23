"""Tests for tools.trace_validate."""
from __future__ import annotations

import pytest

from tools.trace_validate import (
    TraceValidationError,
    assert_traces_valid,
    validate,
)


def _minimal_ledger_with_classification(trace_refs, confluence_refs=(1,), extra_steps=()):
    return {
        "meta": {"schema_version": "1.0", "ticker": "TEST"},
        "setup_classification": {
            "type": "SEPA-VCP",
            "trace_refs": list(trace_refs),
            "confluence_checklist": [
                {
                    "criterion": "test criterion",
                    "status": "PASS",
                    "evidence": "...",
                    "trace_refs": list(confluence_refs),
                }
            ],
        },
        "reasoning_trace": [
            {
                "id": 1,
                "tool": "manual:test",
                "inputs": {},
                "output": True,
                "fetched_at": "2026-05-17T14:30:00Z",
            },
            *extra_steps,
        ],
    }


def test_minimal_valid_ledger():
    ledger = _minimal_ledger_with_classification([1])
    report = validate(ledger)
    assert not report.has_blocks


def test_empty_setup_trace_refs_blocks():
    ledger = _minimal_ledger_with_classification([])
    report = validate(ledger)
    assert report.has_blocks
    assert any(f.code == "empty_trace_refs" for f in report.block_findings)


def test_dangling_setup_trace_ref_blocks():
    ledger = _minimal_ledger_with_classification([99])
    report = validate(ledger)
    assert any(f.code == "dangling_trace_ref" for f in report.block_findings)


def test_confluence_checklist_empty_trace_refs():
    ledger = _minimal_ledger_with_classification([1], confluence_refs=[])
    report = validate(ledger)
    assert any(
        f.code == "empty_trace_refs" and "confluence" in f.location
        for f in report.block_findings
    )


def test_confluence_unknown_status_skipped():
    """UNKNOWN status doesn't require trace_refs."""
    ledger = _minimal_ledger_with_classification([1])
    ledger["setup_classification"]["confluence_checklist"][0]["status"] = "UNKNOWN"
    ledger["setup_classification"]["confluence_checklist"][0]["trace_refs"] = []
    report = validate(ledger)
    assert not report.has_blocks


def test_trace_step_missing_id_blocks():
    ledger = _minimal_ledger_with_classification([1])
    ledger["reasoning_trace"].append(
        {"tool": "manual:x", "inputs": {}, "output": 1, "fetched_at": "2026-05-17T14:30:00Z"}
    )
    report = validate(ledger)
    assert any(f.code == "trace_step_missing_id" for f in report.block_findings)


def test_trace_step_duplicate_id_blocks():
    ledger = _minimal_ledger_with_classification([1])
    ledger["reasoning_trace"].append(
        {
            "id": 1,
            "tool": "manual:dup",
            "inputs": {},
            "output": 1,
            "fetched_at": "2026-05-17T14:30:00Z",
        }
    )
    report = validate(ledger)
    assert any(f.code == "trace_step_duplicate_id" for f in report.block_findings)


def test_ep_specific_trace_required_when_type_ep():
    ledger = _minimal_ledger_with_classification([1])
    ledger["setup_classification"]["type"] = "EP"
    ledger["ep_specific"] = {"gap_pct": 0.14}  # no trace_refs
    report = validate(ledger)
    assert any(
        f.code == "empty_trace_refs" and "ep_specific" in f.location
        for f in report.block_findings
    )


def test_ep_specific_not_required_for_non_ep():
    ledger = _minimal_ledger_with_classification([1])
    # type=SEPA-VCP already; even if ep_specific present, no trace required.
    ledger["ep_specific"] = {"gap_pct": 0.14}
    report = validate(ledger)
    # No block for ep_specific (might still have other blocks; check absence
    # of THAT specific issue).
    ep_blocks = [
        f for f in report.block_findings
        if "ep_specific" in f.location and f.code == "empty_trace_refs"
    ]
    assert ep_blocks == []


def test_position_state_starter_trace_required():
    ledger = _minimal_ledger_with_classification([1])
    ledger["position_state"] = {
        "stage": "STARTER",
        "intended_full_shares": 60,
        "starter": {"shares": 20, "fill_price": 415.80},  # missing trace_refs
    }
    report = validate(ledger)
    assert any(
        f.code == "empty_trace_refs" and "starter" in f.location
        for f in report.block_findings
    )


def test_sell_eval_hold_doesnt_require_trace():
    ledger = _minimal_ledger_with_classification([1])
    ledger["sell_eval_history"] = [
        {"date": "2026-05-20", "action": "hold"},
    ]
    report = validate(ledger)
    assert not report.has_blocks


def test_sell_eval_sell_50_requires_trace():
    ledger = _minimal_ledger_with_classification([1])
    ledger["sell_eval_history"] = [
        {"date": "2026-05-20", "action": "sell_50"},  # missing trace_refs
    ]
    report = validate(ledger)
    assert any(
        f.code == "empty_trace_refs" and "sell_eval_history" in f.location
        for f in report.block_findings
    )


def test_uncited_step_is_warn_only():
    ledger = _minimal_ledger_with_classification(
        [1],
        extra_steps=[
            {
                "id": 2,
                "tool": "manual:unused",
                "inputs": {},
                "output": "value",
                "fetched_at": "2026-05-17T14:30:00Z",
            }
        ],
    )
    report = validate(ledger)
    assert not report.has_blocks  # uncited is WARN
    assert any(f.code == "uncited_trace_step" for f in report.findings)


def test_assert_raises_on_block():
    ledger = _minimal_ledger_with_classification([])
    with pytest.raises(TraceValidationError, match="empty_trace_refs"):
        assert_traces_valid(ledger)


def test_assert_passes_on_warn_only():
    ledger = _minimal_ledger_with_classification(
        [1],
        extra_steps=[
            {
                "id": 2,
                "tool": "manual:unused",
                "inputs": {},
                "output": "value",
                "fetched_at": "2026-05-17T14:30:00Z",
            }
        ],
    )
    entry = assert_traces_valid(ledger)
    assert entry.output["warn_count"] >= 1
