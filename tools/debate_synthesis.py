"""Phase 7 — Gate 6 bull/bear debate synthesis (CLI + library).

The facilitator (``risk-and-compliance``) calls this after Gates 1-5 pass.
The tool:

1. Parses the bear's terminal ```json fragment from the bear Markdown report.
2. Extracts the bull's grade + thesis from the candidate fact ledger.
3. Scores bull/bear strength (or uses caller-provided overrides).
4. Resolves the H1-spec §6 decision table into an :class:`SwingVerdict`.
5. Composes a :class:`DebateState` and writes
   ``ledgers/debate/<TICKER>-<DATE>.yml``.
6. Returns a :class:`TraceEntry` slottable into the candidate ledger's
   ``reasoning_trace``.

Decision-table override order:

* ``already_fired`` risk trigger → REJECT (§6 edge case)
* ``INVALIDATION_WEAK`` bear + ``A+``/``A`` bull grade + all 5 gates passed →
  ENTRY_STRONG (§6 floor case)
* ``(≤3, ≥8)`` → REJECT
* ``(≥8, ≤3)`` → ENTRY_STRONG
* ``(≥6, ≤5)`` → ENTRY_NORMAL
* ``(≤5, ≥6)`` → DEFER
* ``(4-7, 4-7)`` with ``|bull - bear| ≤ 2`` → WATCH_BUILD_THESIS with
  ``failure_mode = balanced_evidence_no_clear_stance``
* Fallback → WATCH_BUILD_THESIS (no failure mode)

CLI::

    uv run python -m tools.debate_synthesis ledgers/candidates/2026-05-25/CEG.yml \\
        --bull ledgers/candidates/2026-05-25/CEG.md \\
        --bear ledgers/candidates/2026-05-25/CEG-bear.md
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .cli import emit
from .contract import (
    BearCase,
    BearVerdict,
    BullCase,
    BullCounterpoint,
    DebateMode,
    DebateState,
    RiskTrigger,
    SwingVerdict,
    SynthesisResult,
    TraceEntry,
)

TOOL = "tools/debate_synthesis.py"

_GRADE_TO_BULL_STRENGTH = {
    "A+": 9,
    "A": 8,
    "B+": 7,
    "B": 6,
    "B-": 5,
    "C+": 4,
    "C": 4,
    "C-": 3,
    "D": 2,
}

_BEAR_VERDICT_TO_STRENGTH = {
    BearVerdict.INVALIDATION_STRONG: 8,
    BearVerdict.INVALIDATION_PARTIAL: 5,
    BearVerdict.INVALIDATION_WEAK: 2,
}


# ---------------------------------------------------------------------------
# Pure decision table — §6
# ---------------------------------------------------------------------------


def verdict_from_strengths(
    bull_strength: int,
    bear_strength: int,
    *,
    already_fired: bool = False,
    bull_grade: str | None = None,
    bear_verdict: BearVerdict | None = None,
    all_gates_passed: bool = True,
) -> tuple[SwingVerdict, str | None]:
    """Map (bull_strength, bear_strength) → (SwingVerdict, failure_mode).

    Implements the H1 spec §6 decision table including the two edge-case
    overrides (already-fired risk trigger → REJECT; INVALIDATION_WEAK +
    A-grade bull + all gates passed → ENTRY_STRONG floor).
    """
    if not (0 <= bull_strength <= 10):
        raise ValueError(f"bull_strength must be in [0, 10]; got {bull_strength}")
    if not (0 <= bear_strength <= 10):
        raise ValueError(f"bear_strength must be in [0, 10]; got {bear_strength}")

    # Override 1: any risk trigger has already fired → REJECT regardless.
    if already_fired:
        return SwingVerdict.REJECT, None

    # Override 2: bear couldn't construct a real invalidation thesis AND
    # the bull is A-tier AND all prior gates passed → ENTRY_STRONG floor.
    if (
        bear_verdict == BearVerdict.INVALIDATION_WEAK
        and bull_grade in ("A+", "A")
        and all_gates_passed
    ):
        return SwingVerdict.ENTRY_STRONG, None

    # Decision table in priority order (strictest extremes first).
    if bull_strength <= 3 and bear_strength >= 8:
        return SwingVerdict.REJECT, None
    if bull_strength >= 8 and bear_strength <= 3:
        return SwingVerdict.ENTRY_STRONG, None
    if bull_strength >= 6 and bear_strength <= 5:
        return SwingVerdict.ENTRY_NORMAL, None
    if bull_strength <= 5 and bear_strength >= 6:
        return SwingVerdict.DEFER, None

    # Balanced band — both in 4-7 AND gap ≤ 2 → WATCH with failure mode.
    if (
        4 <= bull_strength <= 7
        and 4 <= bear_strength <= 7
        and abs(bull_strength - bear_strength) <= 2
    ):
        return SwingVerdict.WATCH_BUILD_THESIS, "balanced_evidence_no_clear_stance"

    # Fallback (uncovered cells — e.g. (0,0), (7,10), (10,7)): plain WATCH.
    return SwingVerdict.WATCH_BUILD_THESIS, None


# ---------------------------------------------------------------------------
# Default heuristic strength scorers (overridable via CLI)
# ---------------------------------------------------------------------------


def default_bull_strength(
    bull_case: BullCase, *, all_gates_passed: bool = True
) -> int:
    base = _GRADE_TO_BULL_STRENGTH.get(bull_case.confluence_grade or "", 4)
    if all_gates_passed:
        base += 1
    return max(0, min(10, base))


def default_bear_strength(bear_case: BearCase) -> int:
    base = _BEAR_VERDICT_TO_STRENGTH[bear_case.verdict]
    fired = sum(1 for t in bear_case.risk_triggers if t.already_fired)
    base += fired  # each already-fired trigger raises bear strength
    return max(0, min(10, base))


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


_JSON_FENCE_RE = re.compile(r"```json\s*(.+?)```", re.DOTALL | re.IGNORECASE)


def parse_bear_json_fragment(markdown_text: str, report_path: str) -> BearCase:
    """Pull the last ```json fenced block out of the bear Markdown and
    parse it into a :class:`BearCase`."""
    matches = _JSON_FENCE_RE.findall(markdown_text)
    if not matches:
        raise ValueError(
            "bear report has no terminal ```json fenced block; cannot parse bear_case"
        )
    raw = matches[-1].strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"bear JSON fragment is not valid JSON: {exc}") from exc

    if "report_path" not in payload:
        payload["report_path"] = report_path
    payload.setdefault("risk_triggers", [])
    payload.setdefault("bull_counterpoints", [])
    payload.setdefault("trace_refs", [])

    return BearCase.model_validate(payload)


def extract_bull_case(
    candidate_ledger: dict[str, Any],
    bull_report_path: str,
    *,
    thesis_one_sentence: str | None = None,
) -> BullCase:
    """Pull bull-side facts from the candidate ledger.

    ``confluence_grade`` comes from ``setup_classification.grade`` (Phase 2.b);
    ``trace_refs`` is the union of all trace IDs referenced by the
    setup_classification block.
    """
    setup = candidate_ledger.get("setup_classification") or {}
    grade = setup.get("grade")

    trace_refs: set[int] = set()
    confluence = setup.get("confluence_checklist") or []
    if isinstance(confluence, list):
        for item in confluence:
            if isinstance(item, dict):
                for ref in item.get("trace_refs", []) or []:
                    if isinstance(ref, int):
                        trace_refs.add(ref)
    for ref in setup.get("trace_refs", []) or []:
        if isinstance(ref, int):
            trace_refs.add(ref)

    if thesis_one_sentence is None:
        thesis_one_sentence = (
            setup.get("thesis_one_sentence")
            or setup.get("narrative")
            or setup.get("reasoning")
            or f"{setup.get('type', 'Unknown')} setup, grade {grade or 'unknown'}."
        )

    return BullCase(
        report_path=bull_report_path,
        thesis_one_sentence=thesis_one_sentence,
        confluence_grade=grade,
        trace_refs=sorted(trace_refs),
    )


# ---------------------------------------------------------------------------
# Compose a DebateState
# ---------------------------------------------------------------------------


def compose(
    candidate_ledger: dict[str, Any],
    bull_case: BullCase,
    bear_case: BearCase,
    *,
    bull_strength: int | None = None,
    bear_strength: int | None = None,
    all_gates_passed: bool = True,
    debate_date: str | None = None,
    candidate_ledger_path: str | None = None,
    asof: datetime | None = None,
) -> DebateState:
    """Compose a :class:`DebateState` from bull + bear cases."""
    meta = candidate_ledger.get("meta") or {}
    ticker = meta.get("ticker")
    if not ticker:
        raise ValueError("candidate ledger has no meta.ticker — cannot compose debate")
    ledger_path = candidate_ledger_path or meta.get("ledger_path") or ""

    if debate_date is None:
        debate_date = date.today().isoformat()

    if bull_strength is None:
        bull_strength = default_bull_strength(
            bull_case, all_gates_passed=all_gates_passed
        )
    if bear_strength is None:
        bear_strength = default_bear_strength(bear_case)

    any_already_fired = any(t.already_fired for t in bear_case.risk_triggers)

    verdict, failure_mode = verdict_from_strengths(
        bull_strength,
        bear_strength,
        already_fired=any_already_fired,
        bull_grade=bull_case.confluence_grade,
        bear_verdict=bear_case.verdict,
        all_gates_passed=all_gates_passed,
    )

    decisive: set[int] = set()
    decisive.update(bull_case.trace_refs)
    for trig in bear_case.risk_triggers:
        decisive.update(trig.trace_refs)
    for cp in bear_case.bull_counterpoints:
        decisive.update(cp.trace_refs)

    rationale = _render_rationale(
        bull_case=bull_case,
        bear_case=bear_case,
        bull_strength=bull_strength,
        bear_strength=bear_strength,
        verdict=verdict,
        failure_mode=failure_mode,
        already_fired=any_already_fired,
    )

    computed_at = (asof or datetime.now(timezone.utc)).isoformat(timespec="seconds")

    synthesis = SynthesisResult(
        facilitator="risk-and-compliance",
        verdict=verdict,
        rationale_one_paragraph=rationale,
        bull_strength_score=bull_strength,
        bear_strength_score=bear_strength,
        decisive_evidence_trace_refs=sorted(decisive),
        failure_mode=failure_mode,
        computed_at=computed_at,
        tool=TOOL,
    )

    return DebateState(
        schema_version="1.0",
        ticker=ticker,
        date=debate_date,
        candidate_ledger_path=ledger_path,
        bull_case=bull_case,
        bear_case=bear_case,
        debate_mode=DebateMode.SINGLE_SHOT,
        synthesis=synthesis,
    )


def _render_rationale(
    *,
    bull_case: BullCase,
    bear_case: BearCase,
    bull_strength: int,
    bear_strength: int,
    verdict: SwingVerdict,
    failure_mode: str | None,
    already_fired: bool,
) -> str:
    pieces = [
        f"Bull strength {bull_strength}/10 (grade {bull_case.confluence_grade or 'n/a'}),"
        f" bear strength {bear_strength}/10 ({bear_case.verdict.value}).",
    ]
    if already_fired:
        pieces.append("One or more risk triggers already fired — override to REJECT.")
    if failure_mode == "balanced_evidence_no_clear_stance":
        pieces.append(
            "Bull and bear differ by ≤2 within the 4-7 band — facilitator could"
            " not reach a clear stance; emit WATCH_BUILD_THESIS for re-evaluation"
            " on next trading day with fresh data."
        )
    pieces.append(f"Verdict: {verdict.value}.")
    return " ".join(pieces)


# ---------------------------------------------------------------------------
# Library + CLI entry points
# ---------------------------------------------------------------------------


def _load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"file not found: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{p}: root must be a YAML mapping; got {type(data).__name__}")
    return data


def gate6_precheck(candidate_ledger_path: str | Path) -> dict[str, Any]:
    """Verify Gate 6 preconditions before risk-and-compliance emits a SwingVerdict.

    Doctrine: every SwingVerdict must be composed from BOTH a bull case
    (trade-researcher) and a bear case (trade-skeptic). Skipping the skeptic
    leaves Gate 6 unrunnable and makes the verdict doctrine-non-compliant.

    Convention (derived from the candidate ledger path):

    - ledger:  ``ledgers/candidates/YYYY-MM-DD/<TICKER>.yml``
    - bull:    ``ledgers/candidates/YYYY-MM-DD/<TICKER>.md``  (trade-researcher)
    - bear:    ``ledgers/candidates/YYYY-MM-DD/<TICKER>-bear.md``  (trade-skeptic)

    Returns a dict with:

    - ``can_proceed``: ``True`` iff all preconditions met
    - ``blockers``: list of human-readable failure reasons (empty when ready)
    - ``bull_report_path`` / ``bear_report_path``: actual paths when present
    - ``expected_bull_path`` / ``expected_bear_path``: where the files MUST be
    - ``candidate_ledger_path``: echo of the input

    risk-and-compliance MUST run this before any other gate and ABORT on
    ``can_proceed=False``. The CLI exits with code 1 when blocked so a
    Bash-driven gate sequence can short-circuit cleanly.
    """
    cl = Path(candidate_ledger_path)
    bull_path = cl.with_suffix(".md")
    bear_path = cl.parent / f"{cl.stem}-bear.md"

    blockers: list[str] = []

    if not cl.exists():
        blockers.append(f"candidate ledger missing: {cl}")

    if not bull_path.exists():
        blockers.append(
            f"bull report (trade-researcher) missing: {bull_path}. "
            "Invoke trade-researcher first and have it write the Markdown "
            "report alongside the candidate ledger."
        )

    if not bear_path.exists():
        blockers.append(
            f"bear report (trade-skeptic) missing: {bear_path}. "
            "Gate 6 doctrine requires the adversarial bear case before "
            "SwingVerdict can be composed. Invoke trade-skeptic with the "
            "candidate ledger first, then re-run."
        )
    else:
        bear_text = bear_path.read_text(encoding="utf-8")
        if not _JSON_FENCE_RE.findall(bear_text):
            blockers.append(
                f"bear report has no terminal ```json fenced block: "
                f"{bear_path}. Re-run trade-skeptic to emit a properly-"
                "formatted bear report with the structured JSON contract."
            )

    return {
        "can_proceed": not blockers,
        "blockers": blockers,
        "bull_report_path": str(bull_path) if bull_path.exists() else None,
        "bear_report_path": str(bear_path) if bear_path.exists() else None,
        "expected_bull_path": str(bull_path),
        "expected_bear_path": str(bear_path),
        "candidate_ledger_path": str(cl),
    }


def compute_from_path(
    candidate_ledger_path: str | Path,
    bull_report_path: str | Path,
    bear_report_path: str | Path,
    *,
    bull_strength: int | None = None,
    bear_strength: int | None = None,
    all_gates_passed: bool = True,
    debate_dir: str | Path = "ledgers/debate",
    debate_date: str | None = None,
    write: bool = True,
    asof: datetime | None = None,
) -> TraceEntry:
    candidate_ledger = _load_yaml(candidate_ledger_path)
    bear_text = Path(bear_report_path).read_text(encoding="utf-8")
    bear_case = parse_bear_json_fragment(bear_text, str(bear_report_path))
    bull_case = extract_bull_case(candidate_ledger, str(bull_report_path))

    state = compose(
        candidate_ledger,
        bull_case,
        bear_case,
        bull_strength=bull_strength,
        bear_strength=bear_strength,
        all_gates_passed=all_gates_passed,
        debate_date=debate_date,
        candidate_ledger_path=str(candidate_ledger_path),
        asof=asof,
    )

    output_path = Path(debate_dir) / f"{state.ticker}-{state.date}.yml"
    if write:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(
                state.model_dump(mode="json"),
                f,
                sort_keys=False,
                allow_unicode=True,
            )

    return TraceEntry(
        tool=TOOL,
        inputs={
            "candidate_ledger_path": str(candidate_ledger_path),
            "bull_report_path": str(bull_report_path),
            "bear_report_path": str(bear_report_path),
            "bull_strength_override": bull_strength,
            "bear_strength_override": bear_strength,
            "all_gates_passed": all_gates_passed,
        },
        output={
            "verdict": state.synthesis.verdict.value,
            "bull_strength": state.synthesis.bull_strength_score,
            "bear_strength": state.synthesis.bear_strength_score,
            "failure_mode": state.synthesis.failure_mode,
            "debate_ledger_path": str(output_path),
            "rationale_one_paragraph": state.synthesis.rationale_one_paragraph,
        },
    )


def _main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m tools.debate_synthesis",
        description="Gate 6 — bull/bear debate synthesis (Phase 7, H1).",
    )
    parser.add_argument(
        "candidate_ledger", help="Path to candidate fact ledger YAML"
    )
    parser.add_argument(
        "--precheck",
        action="store_true",
        help="Precheck mode: verify Gate 6 preconditions (bull AND bear reports "
        "exist + bear has terminal JSON fragment) and exit. Emits the precheck "
        "result as JSON. Exit code 0 = ready; exit code 1 = blocked. "
        "Use this as Gate 0 in risk-and-compliance before any other gate runs.",
    )
    parser.add_argument("--bull", required=False, help="Path to bull (researcher) Markdown report")
    parser.add_argument("--bear", required=False, help="Path to bear (skeptic) Markdown report")
    parser.add_argument(
        "--bull-strength",
        type=int,
        default=None,
        help="Override bull strength score (0-10). Default: heuristic from grade.",
    )
    parser.add_argument(
        "--bear-strength",
        type=int,
        default=None,
        help="Override bear strength score (0-10). Default: heuristic from bear verdict.",
    )
    parser.add_argument(
        "--gates-failed",
        action="store_true",
        help="Pass this flag when one or more of Gates 1-5 failed (suppresses the "
        "INVALIDATION_WEAK + A-grade ENTRY_STRONG floor override).",
    )
    parser.add_argument(
        "--debate-dir",
        default="ledgers/debate",
        help="Directory to write the debate ledger into (default: ledgers/debate).",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Skip writing the debate ledger to disk (still prints the TraceEntry).",
    )
    args = parser.parse_args()

    if args.precheck:
        result = gate6_precheck(args.candidate_ledger)
        print(json.dumps(result, indent=2, sort_keys=True))
        if not result["can_proceed"]:
            raise SystemExit(1)
        return

    if not args.bull or not args.bear:
        parser.error("--bull and --bear are required unless --precheck is set")

    entry = compute_from_path(
        args.candidate_ledger,
        args.bull,
        args.bear,
        bull_strength=args.bull_strength,
        bear_strength=args.bear_strength,
        all_gates_passed=not args.gates_failed,
        debate_dir=args.debate_dir,
        write=not args.no_write,
    )
    emit(entry)


if __name__ == "__main__":
    _main()
