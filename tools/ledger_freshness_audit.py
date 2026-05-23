"""Phase 3 — full ledger freshness audit (CLI + library).

Loads a ledger YAML file, runs :func:`tools.freshness.audit_ledger`, and
emits a verdict slottable into the ledger's ``reasoning_trace`` and
human-readable for the journal.

This is the entry point ``risk-and-compliance`` calls before APPROVE:
any stale section → BLOCK. Warnings (e.g. earnings-blackout proximity)
are surfaced for the agent to weigh.

CLI::

    uv run python -m tools.ledger_freshness_audit ledgers/candidates/2026-05-17/AAPL.yml
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .cli import emit
from .contract import TraceEntry
from .freshness import (
    LedgerFreshnessReport,
    audit_ledger,
)

TOOL = "tools/ledger_freshness_audit.py"


def _load_yaml(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"ledger file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"ledger root must be a YAML mapping; got {type(data).__name__}")
    return data


def _report_to_dict(report: LedgerFreshnessReport) -> dict:
    return {
        "asof_utc": report.asof_utc,
        "overall": report.overall,
        "is_fresh": report.is_fresh,
        "stale_sections": report.stale_sections,
        "sections": [asdict(s) for s in report.sections],
    }


def compute_from_ledger_dict(
    ledger: dict,
    sections: list[str] | None = None,
    asof: datetime | None = None,
) -> TraceEntry:
    report = audit_ledger(ledger, sections=sections, asof=asof)
    return TraceEntry(
        tool=TOOL,
        inputs={
            "ticker": ledger.get("meta", {}).get("ticker"),
            "ledger_path": ledger.get("meta", {}).get("ledger_path"),
            "asof_utc": (asof or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            "sections_requested": sections,
        },
        output=_report_to_dict(report),
    )


def compute_from_path(
    path: str | Path,
    sections: list[str] | None = None,
    asof: datetime | None = None,
) -> TraceEntry:
    ledger = _load_yaml(path)
    entry = compute_from_ledger_dict(ledger, sections=sections, asof=asof)
    entry.inputs["path"] = str(path)
    return entry


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.ledger_freshness_audit",
        description="Audit a ledger YAML for staleness per Requirement 4.",
    )
    p.add_argument("path", help="Path to ledger YAML file")
    p.add_argument(
        "--asof",
        default=None,
        help="ISO timestamp to evaluate against (default: now). e.g. 2026-05-18T14:30:00Z",
    )
    p.add_argument(
        "--section",
        action="append",
        default=None,
        help="Limit audit to specific section(s). Repeat to add multiple.",
    )
    args = p.parse_args()
    asof = None
    if args.asof:
        s = args.asof
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        asof = datetime.fromisoformat(s)
    emit(compute_from_path(args.path, sections=args.section, asof=asof))


if __name__ == "__main__":
    main()
