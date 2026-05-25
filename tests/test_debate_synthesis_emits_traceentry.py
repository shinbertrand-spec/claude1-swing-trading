"""Phase 7 / H1 — ``tools.debate_synthesis.compute_from_path`` returns a
``TraceEntry`` whose shape matches ``tools.contract.TraceEntry``."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from tools.contract import TraceEntry
from tools.debate_synthesis import compute_from_path

REPO = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO / "ledgers" / "debate" / "_schema" / "debate.schema.json"


def _write_minimal_candidate_ledger(path: Path) -> None:
    ledger = {
        "meta": {
            "schema_version": "1.0",
            "ticker": "TEST",
            "asof": "2026-05-25T14:00:00-04:00",
            "state": "candidate",
            "ledger_path": str(path),
            "created_by": "test",
            "created_at": "2026-05-25T14:00:00-04:00",
        },
        "setup_classification": {
            "type": "SEPA-VCP",
            "grade": "A",
            "confluence_checklist": [
                {"item": "Stage 2", "status": "PASS", "trace_refs": [1]},
                {"item": "VCP narrow", "status": "PASS", "trace_refs": [2]},
            ],
            "thesis_one_sentence": "Stage-2 SEPA-VCP on EPS YoY acceleration.",
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(ledger, sort_keys=False), encoding="utf-8")


def _write_bull_report(path: Path) -> None:
    path.write_text(
        "**Ledger:** test\n\n## Bull thesis\n\nStrong setup with A grade.\n",
        encoding="utf-8",
    )


def _write_bear_report(path: Path, *, verdict: str = "INVALIDATION_WEAK", already_fired: bool = False) -> None:
    payload = {
        "report_path": str(path),
        "verdict": verdict,
        "risk_triggers": [
            {
                "condition": "Close below stop",
                "trace_refs": [3],
                "already_fired": already_fired,
            }
        ],
        "bull_counterpoints": [
            {
                "bull_claim_quoted": "Stage 2",
                "counter_evidence": "Borderline",
                "trace_refs": [1],
            }
        ],
        "trace_refs": [3],
    }
    path.write_text(
        "**Bear thesis verdict:** "
        + verdict
        + "\n\n## Sources\n\n```json\n"
        + json.dumps(payload, indent=2)
        + "\n```\n",
        encoding="utf-8",
    )


def test_compute_from_path_returns_traceentry(tmp_path: Path):
    candidate = tmp_path / "candidate.yml"
    _write_minimal_candidate_ledger(candidate)
    bull = tmp_path / "candidate.md"
    bear = tmp_path / "candidate-bear.md"
    _write_bull_report(bull)
    _write_bear_report(bear)

    entry = compute_from_path(
        candidate,
        bull,
        bear,
        debate_dir=tmp_path / "debate",
        debate_date="2026-05-25",
    )
    assert isinstance(entry, TraceEntry)
    assert entry.tool == "tools/debate_synthesis.py"
    assert isinstance(entry.inputs, dict)
    assert "candidate_ledger_path" in entry.inputs
    assert "verdict" in entry.output
    assert "debate_ledger_path" in entry.output
    # Round-trip through to_json: TraceEntry contract.
    payload = json.loads(entry.to_json())
    assert payload["tool"] == "tools/debate_synthesis.py"


def test_compute_from_path_writes_valid_debate_ledger(tmp_path: Path):
    candidate = tmp_path / "candidate.yml"
    _write_minimal_candidate_ledger(candidate)
    bull = tmp_path / "candidate.md"
    bear = tmp_path / "candidate-bear.md"
    _write_bull_report(bull)
    _write_bear_report(bear)

    debate_dir = tmp_path / "debate"
    entry = compute_from_path(
        candidate,
        bull,
        bear,
        debate_dir=debate_dir,
        debate_date="2026-05-25",
    )

    written = Path(entry.output["debate_ledger_path"])
    assert written.exists()
    data = yaml.safe_load(written.read_text(encoding="utf-8"))
    Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))).validate(data)
    assert data["ticker"] == "TEST"
    assert data["date"] == "2026-05-25"


def test_compute_from_path_already_fired_emits_reject(tmp_path: Path):
    candidate = tmp_path / "candidate.yml"
    _write_minimal_candidate_ledger(candidate)
    bull = tmp_path / "candidate.md"
    bear = tmp_path / "candidate-bear.md"
    _write_bull_report(bull)
    _write_bear_report(bear, verdict="INVALIDATION_STRONG", already_fired=True)

    entry = compute_from_path(
        candidate,
        bull,
        bear,
        debate_dir=tmp_path / "debate",
        debate_date="2026-05-25",
    )
    assert entry.output["verdict"] == "REJECT"


def test_compute_from_path_no_json_fragment_raises(tmp_path: Path):
    candidate = tmp_path / "candidate.yml"
    _write_minimal_candidate_ledger(candidate)
    bull = tmp_path / "candidate.md"
    bear = tmp_path / "candidate-bear.md"
    _write_bull_report(bull)
    # bear report missing the terminal json fence
    bear.write_text("just prose, no json fence", encoding="utf-8")

    with pytest.raises(ValueError, match="```json"):
        compute_from_path(
            candidate,
            bull,
            bear,
            debate_dir=tmp_path / "debate",
            debate_date="2026-05-25",
        )
