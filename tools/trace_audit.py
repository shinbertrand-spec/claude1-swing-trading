"""Phase 4 — composite reasoning-trace audit.

Runs all three Phase 4 checks in sequence:

1. :mod:`tools.trace_validate` — completeness + targeting (every
   load-bearing claim cites a real ``reasoning_trace`` step).
2. :mod:`tools.trace_rerun` — re-run pure-arithmetic cited tools;
   shape-check OHLCV-consuming tools.
3. :mod:`tools.claim_extract` — optional cross-reference of a prose
   report against the ledger.

Verdict is **BLOCK** iff:

* trace_validate has any BLOCK finding, OR
* trace_rerun has any divergent/shape_fail result.

Unmatched claims from claim_extract surface as **WARNING** rather than
BLOCK — prose drift is real risk but is downstream of the ledger; the
ledger is the source of truth.

CLI::

    uv run python -m tools.trace_audit ledgers/candidates/2026-05-17/AAPL.yml
    uv run python -m tools.trace_audit ledger.yml --report researcher-report.md
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from .cli import emit
from .contract import TraceEntry
from .claim_extract import extract as claim_extract
from .trace_rerun import rerun as trace_rerun
from .trace_validate import validate as trace_validate

TOOL = "tools/trace_audit.py"


@dataclass
class TraceAuditVerdict:
    overall: str             # "APPROVE" | "BLOCK"
    block_reasons: list[str]
    warn_reasons: list[str]


def audit(ledger: dict, report_text: str | None = None) -> dict:
    validate_report = trace_validate(ledger)
    rerun_report = trace_rerun(ledger)

    block_reasons: list[str] = []
    warn_reasons: list[str] = []

    if validate_report.has_blocks:
        for f in validate_report.block_findings:
            block_reasons.append(f"validate:{f.code} @ {f.location} — {f.message}")
    if rerun_report.has_divergence:
        for r in rerun_report.results:
            if r.status in {"divergent", "shape_fail"}:
                block_reasons.append(
                    f"rerun:{r.status} step={r.step_id} tool={r.tool} — {r.detail}"
                )
    # WARN findings from validate (uncited steps etc.)
    for f in validate_report.findings:
        if f.severity == "WARN":
            warn_reasons.append(f"validate:{f.code} — {f.message}")
    # Unknown tool classifications are WARN (might be legitimate manual
    # source that wasn't tagged manual:* properly).
    for r in rerun_report.results:
        if r.status == "unknown_tool":
            warn_reasons.append(f"rerun:unknown_tool step={r.step_id} tool={r.tool}")

    claim_report = None
    if report_text is not None:
        claim_report = claim_extract(report_text, ledger)
        if claim_report.unmatched:
            warn_reasons.append(
                f"claim_extract: {len(claim_report.unmatched)} numeric claim(s) "
                f"in prose without ledger match"
            )

    verdict = TraceAuditVerdict(
        overall="BLOCK" if block_reasons else "APPROVE",
        block_reasons=block_reasons,
        warn_reasons=warn_reasons,
    )

    out: dict = {
        "verdict": asdict(verdict),
        "validate": {
            "has_blocks": validate_report.has_blocks,
            "trace_step_ids": validate_report.trace_step_ids,
            "cited_ids": validate_report.cited_ids,
            "uncited_ids": validate_report.uncited_ids,
            "findings": [
                {
                    "severity": f.severity,
                    "code": f.code,
                    "location": f.location,
                    "message": f.message,
                }
                for f in validate_report.findings
            ],
        },
        "rerun": {
            "divergent_count": rerun_report.divergent_count,
            "has_divergence": rerun_report.has_divergence,
            "results": [
                {
                    "step_id": r.step_id,
                    "tool": r.tool,
                    "klass": r.klass,
                    "status": r.status,
                    "detail": r.detail,
                }
                for r in rerun_report.results
            ],
        },
    }
    if claim_report is not None:
        out["claim_extract"] = {
            "claim_count": len(claim_report.claims),
            "unmatched_count": len(claim_report.unmatched),
            "unmatched_claims": [
                {
                    "raw_text": c.raw_text,
                    "value": c.value,
                    "line": c.line,
                    "column": c.column,
                }
                for c in claim_report.unmatched
            ],
        }
    return out


def compute_from_path(
    ledger_path: str | Path,
    report_path: str | Path | None = None,
) -> TraceEntry:
    ledger = yaml.safe_load(Path(ledger_path).read_text(encoding="utf-8"))
    report_text = None
    if report_path is not None:
        report_text = Path(report_path).read_text(encoding="utf-8")
    out = audit(ledger, report_text)
    return TraceEntry(
        tool=TOOL,
        inputs={
            "ledger_path": str(ledger_path),
            "report_path": str(report_path) if report_path else None,
            "ticker": ledger.get("meta", {}).get("ticker"),
        },
        output=out,
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.trace_audit",
        description="Composite Phase 4 reasoning-trace audit (validate + rerun + claim_extract).",
    )
    p.add_argument("ledger", help="Path to ledger YAML")
    p.add_argument("--report", default=None, help="Optional prose report path for claim cross-reference")
    args = p.parse_args()
    emit(compute_from_path(args.ledger, args.report))


if __name__ == "__main__":
    main()
