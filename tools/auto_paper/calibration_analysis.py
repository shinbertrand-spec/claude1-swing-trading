"""Phase-3 calibration analysis - does the swing-critic panel discriminate?

Closes the analysis half of the calibration loop. The panel runs in SHADOW
MODE (verdict logged, sizing NOT applied) until its verdicts are shown to
correlate with realized P&L. This tool joins:

  * entry-time VERDICT records (``append_calibration_log`` - no ``record_type``)
  * realized OUTCOME records (``record_calibration_outcome`` - ``record_type:
    "outcome"``, written by ``reconcile`` when a position closes)

on ``panel_call_id``, then groups realized R / P&L by the panel's verdict
action. The flip-to-live decision is justified when, on a sufficient sample,
the panel SEPARATES winners from losers - i.e. ``preserve`` trades realize
better R than ``half_size_review`` trades (the panel's low-confidence bucket).

CLI::

    uv run python -m tools.auto_paper.calibration_analysis
    uv run python -m tools.auto_paper.calibration_analysis --json

Library::

    from tools.auto_paper.calibration_analysis import compute
    report = compute()
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[2]
_CALIBRATION_DIR = _ROOT / "ledgers" / "swing-critics" / "_calibration"

# Minimum joined (verdict<->outcome) sample before the discrimination signal is
# considered load-bearing. Below this, the tool reports "insufficient data".
MIN_SAMPLE_FOR_FLIP = 20

# Action order from highest panel confidence to lowest. A discriminating panel
# should show realized R monotonically non-increasing down this list.
_ACTION_ORDER = ["preserve", "reduce_20", "half_size_review"]


@dataclass
class ActionStats:
    action: str
    n: int
    n_wins: int
    win_rate: Optional[float]
    avg_realized_r: Optional[float]
    total_pnl: float
    avg_pnl: Optional[float]


@dataclass
class CalibrationReport:
    n_verdict_records: int
    n_outcome_records: int
    n_joined: int                       # outcomes matched to a verdict
    n_unmatched_outcomes: int           # closes with no panel verdict (manual/pre-panel)
    by_action: dict[str, ActionStats]
    discrimination: str                 # human-readable verdict
    ready_to_flip: bool
    notes: list[str] = field(default_factory=list)
    # Evaluation-honesty policy A4: warmup/test split. Verdicts computed
    # before `scored_start` (and their outcomes) build context but are NOT
    # scored. Both dates recorded in the artifact; None = no split applied
    # (pre-policy behaviour: everything scored).
    warmup_start: Optional[str] = None
    scored_start: Optional[str] = None
    n_warmup_verdicts: int = 0
    n_warmup_outcomes: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["by_action"] = {k: asdict(v) for k, v in self.by_action.items()}
        return d


def _load_records(cal_dir: Path) -> tuple[list[dict], list[dict]]:
    """Return (verdict_records, outcome_records) across all calibration files."""
    verdicts: list[dict] = []
    outcomes: list[dict] = []
    if not cal_dir.is_dir():
        return verdicts, outcomes
    for fp in sorted(cal_dir.glob("*.jsonl")):
        with open(fp, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("record_type") == "outcome":
                    outcomes.append(rec)
                else:
                    verdicts.append(rec)
    return verdicts, outcomes


def compute(
    cal_dir: Path | None = None,
    *,
    warmup_start: str | None = None,
    scored_start: str | None = None,
) -> CalibrationReport:
    """Join verdicts<->outcomes on panel_call_id and group realized stats by action.

    Args:
        cal_dir: calibration log directory (default: the live ledger dir).
        warmup_start: informational label for when the warmup window began
            (recorded in the artifact; does not affect the split).
        scored_start: evaluation-honesty policy A4 — verdicts whose
            ``computed_at`` is before this ISO date are WARMUP: the panel was
            still building context, so they (and their outcomes) are counted
            but excluded from the scored discrimination stats. None = no
            split (all records scored).
    """
    if cal_dir is None:
        cal_dir = _CALIBRATION_DIR
    verdicts, outcomes = _load_records(cal_dir)

    # A4 split: partition verdicts by computed_at vs scored_start.
    warmup_call_ids: set[str] = set()
    n_warmup_verdicts = 0
    if scored_start:
        scored_from = scored_start[:10]
        kept: list[dict] = []
        for v in verdicts:
            if str(v.get("computed_at") or "")[:10] < scored_from:
                n_warmup_verdicts += 1
                cid = v.get("panel_call_id")
                if cid:
                    warmup_call_ids.add(cid)
            else:
                kept.append(v)
        verdicts_scored = kept
    else:
        verdicts_scored = verdicts

    # Verdict action by panel_call_id (last write wins - verdicts are unique
    # per call_id, but be defensive).
    action_by_call: dict[str, str] = {}
    for v in verdicts_scored:
        cid = v.get("panel_call_id")
        if cid:
            action_by_call[cid] = v.get("action")

    # Bucket joined outcomes by action.
    buckets: dict[str, list[dict]] = {}
    n_joined = 0
    n_unmatched = 0
    n_warmup_outcomes = 0
    for o in outcomes:
        cid = o.get("panel_call_id")
        # Outcome of a warmup verdict → warmup (the judgment being scored
        # was made during context-building), regardless of close date.
        if cid and cid in warmup_call_ids:
            n_warmup_outcomes += 1
            continue
        action = action_by_call.get(cid) if cid else None
        # Fall back to the action the outcome record copied at close time.
        action = action or o.get("verdict_action")
        if action is None:
            n_unmatched += 1
            continue
        # Fallback-joined outcomes (no verdict record) split on entry_date.
        if (
            scored_start
            and (not cid or cid not in action_by_call)
            and str(o.get("entry_date") or "")[:10]
            and str(o.get("entry_date") or "")[:10] < scored_start[:10]
        ):
            n_warmup_outcomes += 1
            continue
        n_joined += 1
        buckets.setdefault(action, []).append(o)

    by_action: dict[str, ActionStats] = {}
    for action, rows in buckets.items():
        rs = [r["realized_r"] for r in rows if r.get("realized_r") is not None]
        pnls = [float(r.get("realized_pnl") or 0.0) for r in rows]
        n = len(rows)
        n_wins = sum(1 for r in rs if r > 0)
        by_action[action] = ActionStats(
            action=action,
            n=n,
            n_wins=n_wins,
            win_rate=round(n_wins / len(rs), 4) if rs else None,
            avg_realized_r=round(sum(rs) / len(rs), 4) if rs else None,
            total_pnl=round(sum(pnls), 2),
            avg_pnl=round(sum(pnls) / n, 2) if n else None,
        )

    discrimination, ready, notes = _assess(by_action, n_joined)
    return CalibrationReport(
        n_verdict_records=len(verdicts),
        n_outcome_records=len(outcomes),
        n_joined=n_joined,
        n_unmatched_outcomes=n_unmatched,
        by_action=by_action,
        discrimination=discrimination,
        ready_to_flip=ready,
        notes=notes,
        warmup_start=warmup_start,
        scored_start=scored_start,
        n_warmup_verdicts=n_warmup_verdicts,
        n_warmup_outcomes=n_warmup_outcomes,
    )


def _assess(
    by_action: dict[str, ActionStats], n_joined: int,
) -> tuple[str, bool, list[str]]:
    """Decide whether the panel discriminates + whether the flip is justified."""
    notes: list[str] = []
    if n_joined < MIN_SAMPLE_FOR_FLIP:
        return (
            f"INSUFFICIENT DATA - {n_joined} joined closed trades "
            f"(need >= {MIN_SAMPLE_FOR_FLIP}). Keep panel in shadow mode.",
            False,
            ["Calibration loop is now capturing outcomes; re-run after more "
             "paper-auto positions close."],
        )

    preserve = by_action.get("preserve")
    half = by_action.get("half_size_review")
    if not preserve or not half or preserve.avg_realized_r is None or half.avg_realized_r is None:
        return (
            "INCONCLUSIVE - need closed trades in BOTH the preserve and "
            "half_size_review buckets to compare.",
            False, notes,
        )

    # The core test: does the panel's low-confidence bucket realize worse R?
    edge = round(preserve.avg_realized_r - half.avg_realized_r, 4)
    if edge > 0:
        notes.append(
            f"preserve avg_R {preserve.avg_realized_r} > half_size_review "
            f"avg_R {half.avg_realized_r} (edge {edge}R) - panel separates "
            f"winners from its low-confidence bucket."
        )
        return (
            f"PANEL DISCRIMINATES (edge {edge}R over {n_joined} trades) - "
            f"flipping sizing live is justified.",
            True, notes,
        )
    return (
        f"PANEL DOES NOT DISCRIMINATE (preserve avg_R {preserve.avg_realized_r} "
        f"<= half_size_review {half.avg_realized_r}) - do NOT flip; the panel's "
        f"confidence is not predicting realized R.",
        False, notes,
    )


def render(report: CalibrationReport) -> str:
    lines = [
        "# Swing-critic panel - calibration analysis",
        "",
        f"- Verdict records: {report.n_verdict_records}",
        f"- Outcome records: {report.n_outcome_records}",
        f"- Joined (verdict<->outcome): {report.n_joined}",
        f"- Unmatched closes (no panel verdict): {report.n_unmatched_outcomes}",
    ]
    if report.scored_start:
        lines.append(
            f"- Warmup/test split (A4): warmup {report.warmup_start or 'log start'}"
            f" -> {report.scored_start} (excluded: {report.n_warmup_verdicts}"
            f" verdicts, {report.n_warmup_outcomes} outcomes); scored from"
            f" {report.scored_start}"
        )
    lines += [
        "",
        "| Verdict action | n | win% | avg R | total P&L | avg P&L |",
        "|---|---|---|---|---|---|",
    ]
    for action in _ACTION_ORDER + [
        a for a in report.by_action if a not in _ACTION_ORDER
    ]:
        s = report.by_action.get(action)
        if not s:
            continue
        wr = f"{s.win_rate:.0%}" if s.win_rate is not None else "-"
        ar = f"{s.avg_realized_r:+.2f}" if s.avg_realized_r is not None else "-"
        ap = f"${s.avg_pnl:,.0f}" if s.avg_pnl is not None else "-"
        lines.append(
            f"| {action} | {s.n} | {wr} | {ar} | ${s.total_pnl:,.0f} | {ap} |"
        )
    lines += [
        "",
        f"**Discrimination:** {report.discrimination}",
        f"**Ready to flip panel sizing live:** {'YES' if report.ready_to_flip else 'NO'}",
    ]
    for n in report.notes:
        lines.append(f"- {n}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="tools.auto_paper.calibration_analysis",
        description="Join panel verdicts with realized outcomes; assess the flip gate.",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown.")
    p.add_argument(
        "--scored-start", default=None,
        help="ISO date; verdicts computed before this are unscored warmup (A4).",
    )
    p.add_argument(
        "--warmup-start", default=None,
        help="ISO date the warmup window began (recorded in the artifact).",
    )
    args = p.parse_args(argv)
    report = compute(warmup_start=args.warmup_start, scored_start=args.scored_start)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
