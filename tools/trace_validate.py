"""Phase 4 — reasoning-trace completeness + targeting validator.

Per ``swing-risk-compliance-doctrine.md`` Requirement 3 (unfaithful-reasoning
detection). This module enforces the two **structural** invariants that
make a ledger's ``reasoning_trace`` load-bearing:

1. **Completeness** — every conclusion that the framework treats as
   load-bearing carries a non-empty ``trace_refs[]``. Specifically:

   * ``setup_classification.trace_refs`` non-empty
   * ``setup_classification.confluence_checklist[].trace_refs`` non-empty
   * ``position_state.starter.trace_refs`` non-empty (and similarly for
     ``addon_1`` / ``addon_2`` when present)
   * ``ep_specific.trace_refs`` non-empty (when ``setup_classification.type == "EP"``)
   * ``sell_eval_history[].trace_refs`` non-empty

2. **Targeting** — every integer in any ``trace_refs[]`` points to an
   actual entry in ``reasoning_trace[]`` (by ``id``). No dangling refs.
   Trace steps' ``id``s are unique.

The semantic re-run check (do the cited tools' outputs match a re-run?)
is a separate concern handled by :mod:`tools.trace_rerun`.

Used by ``risk-and-compliance`` before APPROVE: failures here = BLOCK.

CLI: not provided directly — see :mod:`tools.trace_audit` for the
composite Phase 4 entry point.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contract import TraceEntry

TOOL = "tools/trace_validate.py"

# Sections where missing/empty trace_refs is structurally fatal.
# Mapping describes how to walk into each section.
LOAD_BEARING_PATHS: tuple[tuple[str, ...], ...] = (
    ("setup_classification", "trace_refs"),
    ("ep_specific", "trace_refs"),
    ("position_state", "starter", "trace_refs"),
    ("position_state", "addon_1", "trace_refs"),
    ("position_state", "addon_2", "trace_refs"),
)


class TraceValidationError(RuntimeError):
    """Raised when the ledger fails structural trace validation."""


@dataclass
class TraceFinding:
    """A single validator finding."""

    severity: str        # "BLOCK" | "WARN"
    code: str            # short machine-readable identifier
    location: str        # dotted path within the ledger
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class TraceValidationReport:
    """Aggregate of completeness + targeting findings."""

    findings: list[TraceFinding]
    trace_step_ids: list[int]
    cited_ids: list[int]
    uncited_ids: list[int]

    @property
    def has_blocks(self) -> bool:
        return any(f.severity == "BLOCK" for f in self.findings)

    @property
    def block_findings(self) -> list[TraceFinding]:
        return [f for f in self.findings if f.severity == "BLOCK"]


def _get_by_path(d: dict, path: tuple[str, ...]) -> Any:
    """Walk a dotted path; return ``None`` if any segment missing or non-dict."""
    cur: Any = d
    for seg in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
        if cur is None:
            return None
    return cur


def _trace_steps(ledger: dict) -> tuple[list[dict], list[TraceFinding]]:
    """Extract ``reasoning_trace`` + validate per-step shape (id uniqueness)."""
    findings: list[TraceFinding] = []
    raw = ledger.get("reasoning_trace") or []
    if not isinstance(raw, list):
        findings.append(
            TraceFinding(
                severity="BLOCK",
                code="trace_not_a_list",
                location="reasoning_trace",
                message="reasoning_trace must be a YAML/JSON list",
            )
        )
        return [], findings
    ids_seen: set[int] = set()
    valid: list[dict] = []
    for i, step in enumerate(raw):
        if not isinstance(step, dict):
            findings.append(
                TraceFinding(
                    severity="BLOCK",
                    code="trace_step_not_a_dict",
                    location=f"reasoning_trace[{i}]",
                    message=f"trace step at index {i} is not a dict",
                )
            )
            continue
        sid = step.get("id")
        if not isinstance(sid, int):
            findings.append(
                TraceFinding(
                    severity="BLOCK",
                    code="trace_step_missing_id",
                    location=f"reasoning_trace[{i}]",
                    message=f"trace step missing integer id (got {sid!r})",
                )
            )
            continue
        if sid in ids_seen:
            findings.append(
                TraceFinding(
                    severity="BLOCK",
                    code="trace_step_duplicate_id",
                    location=f"reasoning_trace[{i}]",
                    message=f"duplicate trace step id {sid}",
                )
            )
            continue
        for req in ("tool", "output", "fetched_at"):
            if req not in step:
                findings.append(
                    TraceFinding(
                        severity="BLOCK",
                        code="trace_step_missing_required_field",
                        location=f"reasoning_trace[{i}]",
                        message=f"trace step id={sid} missing required field {req!r}",
                    )
                )
        ids_seen.add(sid)
        valid.append(step)
    return valid, findings


def _validate_refs(
    refs: Any,
    valid_ids: set[int],
    location: str,
) -> list[TraceFinding]:
    """Verify ``refs`` is a non-empty list of ints all pointing into ``valid_ids``."""
    findings: list[TraceFinding] = []
    if not isinstance(refs, list) or len(refs) == 0:
        findings.append(
            TraceFinding(
                severity="BLOCK",
                code="empty_trace_refs",
                location=location,
                message=(
                    "trace_refs is missing or empty — load-bearing claim has no "
                    "tool-output provenance; unfaithful by Requirement 3"
                ),
            )
        )
        return findings
    for r in refs:
        if not isinstance(r, int):
            findings.append(
                TraceFinding(
                    severity="BLOCK",
                    code="non_integer_trace_ref",
                    location=location,
                    message=f"trace_ref {r!r} is not an integer",
                )
            )
            continue
        if r not in valid_ids:
            findings.append(
                TraceFinding(
                    severity="BLOCK",
                    code="dangling_trace_ref",
                    location=location,
                    message=(
                        f"trace_ref {r} does not point to any reasoning_trace step "
                        f"(valid ids: {sorted(valid_ids)})"
                    ),
                )
            )
    return findings


def _validate_confluence(
    ledger: dict, valid_ids: set[int]
) -> list[TraceFinding]:
    findings: list[TraceFinding] = []
    # Distinguish "key absent" (handled by LOAD_BEARING_PATHS) from "key
    # present with wrong type" (a structural failure — e.g. checklist set
    # to a string defeats validation entirely).
    setup = _get_by_path(ledger, ("setup_classification",))
    if isinstance(setup, dict) and "confluence_checklist" in setup:
        checklist = setup["confluence_checklist"]
        if not isinstance(checklist, list):
            findings.append(
                TraceFinding(
                    severity="BLOCK",
                    code="confluence_checklist_wrong_type",
                    location="setup_classification.confluence_checklist",
                    message=(
                        f"confluence_checklist must be a list, got "
                        f"{type(checklist).__name__}"
                    ),
                )
            )
            return findings
    else:
        return findings
    statuses_seen: list[str] = []
    for i, item in enumerate(checklist):
        if not isinstance(item, dict):
            findings.append(
                TraceFinding(
                    severity="BLOCK",
                    code="confluence_item_not_dict",
                    location=f"setup_classification.confluence_checklist[{i}]",
                    message="confluence checklist item is not a dict",
                )
            )
            continue
        status = item.get("status")
        statuses_seen.append(str(status))
        if status in ("PASS", "FAIL", "PARTIAL"):
            # Only PASS / FAIL / PARTIAL claims need trace; UNKNOWN does not.
            findings.extend(
                _validate_refs(
                    item.get("trace_refs"),
                    valid_ids,
                    location=(
                        f"setup_classification.confluence_checklist[{i}] "
                        f"(criterion={item.get('criterion', '<unnamed>')!r})"
                    ),
                )
            )
    # If every item is UNKNOWN, the agent is making a load-bearing
    # classification with zero evidence. Warn — not block, in case there's
    # a legitimate early-stage research workflow that this would interrupt.
    if statuses_seen and all(s == "UNKNOWN" for s in statuses_seen):
        findings.append(
            TraceFinding(
                severity="WARN",
                code="all_unknown_confluence",
                location="setup_classification.confluence_checklist",
                message=(
                    f"all {len(statuses_seen)} confluence checklist items are UNKNOWN — "
                    "load-bearing setup_classification is being made with no evidence; "
                    "either resolve the criteria or downgrade the classification"
                ),
            )
        )
    return findings


def _validate_sell_history(
    ledger: dict, valid_ids: set[int]
) -> list[TraceFinding]:
    findings: list[TraceFinding] = []
    history = ledger.get("sell_eval_history")
    if not isinstance(history, list):
        return findings
    for i, entry in enumerate(history):
        if not isinstance(entry, dict):
            continue
        # Hold actions don't require trace_refs (nothing happened); other
        # actions do.
        action = entry.get("action")
        if action and action != "hold":
            findings.extend(
                _validate_refs(
                    entry.get("trace_refs"),
                    valid_ids,
                    location=f"sell_eval_history[{i}] (action={action!r})",
                )
            )
    return findings


def _validate_ep_specific(
    ledger: dict, valid_ids: set[int]
) -> list[TraceFinding]:
    """ep_specific.trace_refs is only required when setup type is EP."""
    findings: list[TraceFinding] = []
    setup_type = _get_by_path(ledger, ("setup_classification", "type"))
    ep = ledger.get("ep_specific")
    if setup_type == "EP" and isinstance(ep, dict):
        findings.extend(
            _validate_refs(
                ep.get("trace_refs"),
                valid_ids,
                location="ep_specific.trace_refs (setup_classification.type == 'EP')",
            )
        )
    return findings


def _validate_load_bearing_paths(
    ledger: dict, valid_ids: set[int]
) -> list[TraceFinding]:
    """For each path in LOAD_BEARING_PATHS, if the section exists, the path
    must terminate at a non-empty trace_refs list pointing into valid_ids."""
    findings: list[TraceFinding] = []
    for path in LOAD_BEARING_PATHS:
        # Walk up to the parent of trace_refs.
        section_path = path[:-1]
        section = _get_by_path(ledger, section_path)
        if section is None:
            continue
        # Skip the EP-specific path here — handled with type guard in
        # _validate_ep_specific.
        if path == ("ep_specific", "trace_refs"):
            continue
        refs = section.get("trace_refs") if isinstance(section, dict) else None
        findings.extend(
            _validate_refs(refs, valid_ids, location=".".join(path))
        )
    return findings


def validate(ledger: dict) -> TraceValidationReport:
    """Run completeness + targeting checks. Returns a report."""
    findings: list[TraceFinding] = []

    trace_steps, step_findings = _trace_steps(ledger)
    findings.extend(step_findings)
    valid_ids = {s["id"] for s in trace_steps}

    # A ledger that omits every load-bearing section has nothing to validate
    # and would silently pass. At least one of these must be present.
    load_bearing_present = any(
        isinstance(ledger.get(s), dict)
        for s in ("setup_classification", "position_state", "ep_specific")
    )
    if not load_bearing_present:
        findings.append(
            TraceFinding(
                severity="BLOCK",
                code="no_load_bearing_section",
                location="<root>",
                message=(
                    "ledger must contain at least one of setup_classification, "
                    "position_state, or ep_specific — otherwise there is no "
                    "load-bearing claim to validate"
                ),
            )
        )

    findings.extend(_validate_load_bearing_paths(ledger, valid_ids))
    findings.extend(_validate_confluence(ledger, valid_ids))
    findings.extend(_validate_ep_specific(ledger, valid_ids))
    findings.extend(_validate_sell_history(ledger, valid_ids))

    # Collect every cited id for the uncited-step warning.
    cited: set[int] = set()
    for path in LOAD_BEARING_PATHS:
        refs = _get_by_path(ledger, path)
        if isinstance(refs, list):
            cited.update(r for r in refs if isinstance(r, int))
    checklist = _get_by_path(ledger, ("setup_classification", "confluence_checklist")) or []
    for item in checklist if isinstance(checklist, list) else []:
        if isinstance(item, dict):
            for r in item.get("trace_refs") or []:
                if isinstance(r, int):
                    cited.add(r)
    history = ledger.get("sell_eval_history") or []
    for item in history if isinstance(history, list) else []:
        if isinstance(item, dict):
            for r in item.get("trace_refs") or []:
                if isinstance(r, int):
                    cited.add(r)

    uncited = sorted(valid_ids - cited)
    if uncited:
        findings.append(
            TraceFinding(
                severity="WARN",
                code="uncited_trace_step",
                location="reasoning_trace",
                message=(
                    f"{len(uncited)} trace step(s) recorded but never cited by any "
                    f"load-bearing claim: ids={uncited}. Either drop them or cite them."
                ),
                detail={"uncited_ids": uncited},
            )
        )

    return TraceValidationReport(
        findings=findings,
        trace_step_ids=sorted(valid_ids),
        cited_ids=sorted(cited),
        uncited_ids=uncited,
    )


def compute(ledger: dict) -> TraceEntry:
    """Wrap :func:`validate` in the :class:`TraceEntry` contract."""
    report = validate(ledger)
    return TraceEntry(
        tool=TOOL,
        inputs={
            "trace_step_count": len(report.trace_step_ids),
            "cited_id_count": len(report.cited_ids),
        },
        output={
            "block_count": len(report.block_findings),
            "warn_count": len([f for f in report.findings if f.severity == "WARN"]),
            "has_blocks": report.has_blocks,
            "findings": [
                {
                    "severity": f.severity,
                    "code": f.code,
                    "location": f.location,
                    "message": f.message,
                    "detail": f.detail,
                }
                for f in report.findings
            ],
            "trace_step_ids": report.trace_step_ids,
            "cited_ids": report.cited_ids,
            "uncited_ids": report.uncited_ids,
        },
    )


def assert_traces_valid(ledger: dict) -> TraceEntry:
    """Run :func:`compute`; raise :class:`TraceValidationError` on any BLOCK."""
    entry = compute(ledger)
    if entry.output["has_blocks"]:
        first_block = next(
            f for f in entry.output["findings"] if f["severity"] == "BLOCK"
        )
        raise TraceValidationError(
            f"trace validation failed at {first_block['location']}: "
            f"{first_block['code']} — {first_block['message']}"
        )
    return entry
