"""Tests for tools.trace_audit."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.trace_audit import audit, compute_from_path


def _good_ledger():
    return {
        "meta": {"schema_version": "1.0", "ticker": "TEST"},
        "setup_classification": {
            "type": "SEPA-VCP",
            "trace_refs": [1],
            "confluence_checklist": [
                {
                    "criterion": "x",
                    "status": "PASS",
                    "evidence": "...",
                    "trace_refs": [1],
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
            }
        ],
    }


def test_good_ledger_approves():
    out = audit(_good_ledger())
    assert out["verdict"]["overall"] == "APPROVE"
    assert out["verdict"]["block_reasons"] == []


def test_empty_setup_trace_refs_blocks():
    ledger = _good_ledger()
    ledger["setup_classification"]["trace_refs"] = []
    out = audit(ledger)
    assert out["verdict"]["overall"] == "BLOCK"
    assert any("empty_trace_refs" in r for r in out["verdict"]["block_reasons"])


def test_divergent_pure_tool_blocks():
    ledger = _good_ledger()
    # Add a pure-tool trace step with wrong output.
    ledger["reasoning_trace"].append(
        {
            "id": 2,
            "tool": "tools/compute_yoy.py",
            "inputs": {"current": 1.87, "prior": 1.55},
            "output": {"yoy_growth_decimal": 99.0, "yoy_growth_pct": 9900.0},
            "fetched_at": "2026-05-17T14:30:00Z",
        }
    )
    ledger["setup_classification"]["trace_refs"].append(2)
    out = audit(ledger)
    assert out["verdict"]["overall"] == "BLOCK"
    assert any("divergent" in r for r in out["verdict"]["block_reasons"])


def test_unknown_tool_is_warn_not_block():
    ledger = _good_ledger()
    ledger["reasoning_trace"].append(
        {
            "id": 2,
            "tool": "tools/nonexistent.py",
            "inputs": {},
            "output": None,
            "fetched_at": "2026-05-17T14:30:00Z",
        }
    )
    out = audit(ledger)
    # uncited_trace_step warn AND unknown_tool warn — both are WARN; overall APPROVE.
    assert out["verdict"]["overall"] == "APPROVE"
    assert any("unknown_tool" in r for r in out["verdict"]["warn_reasons"])


def test_claim_cross_reference_unmatched_warns():
    ledger = _good_ledger()
    ledger["quote"] = {"last": 192.74}
    report_text = "We saw a great trade at $999.99 today."
    out = audit(ledger, report_text)
    assert "claim_extract" in out
    assert out["claim_extract"]["unmatched_count"] >= 1
    assert any("claim_extract" in r for r in out["verdict"]["warn_reasons"])


def test_compute_from_path_round_trips(tmp_path: Path):
    ledger = _good_ledger()
    p = tmp_path / "TEST.yml"
    p.write_text(yaml.safe_dump(ledger), encoding="utf-8")
    entry = compute_from_path(p)
    assert entry.output["verdict"]["overall"] == "APPROVE"
    assert entry.inputs["ticker"] == "TEST"


def test_audit_against_example_sepa_vcp_ledger():
    path = Path("ledgers/_examples/sepa-vcp-candidate.yml")
    if not path.exists():
        pytest.skip(f"example ledger not present at {path}")
    entry = compute_from_path(path)
    # Example ledger is hand-built to satisfy the structural contract.
    assert entry.output["verdict"]["overall"] in {"APPROVE", "BLOCK"}
    # Specifically: trace_validate must not find structural problems on
    # the curated example.
    assert not entry.output["validate"]["has_blocks"], (
        f"example ledger failed validate: "
        f"{[f for f in entry.output['validate']['findings'] if f['severity'] == 'BLOCK']}"
    )


def test_audit_against_example_pyramided_position():
    path = Path("ledgers/_examples/pyramided-position.yml")
    if not path.exists():
        pytest.skip(f"example ledger not present at {path}")
    entry = compute_from_path(path)
    # Curated example should satisfy the structural contract too.
    assert not entry.output["validate"]["has_blocks"], (
        f"pyramided example failed validate: "
        f"{[f for f in entry.output['validate']['findings'] if f['severity'] == 'BLOCK']}"
    )
