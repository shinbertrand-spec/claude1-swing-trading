"""Multi-rater swing-critic panel aggregator (Phase 3).

Pure-Python deterministic aggregator that composes N adversarial-critic
votes (per `.claude/agents/swing-critics/*.md`) into a single
:class:`PanelVerdict`. The verdict carries a ``sizing_multiplier`` and an
``action`` recommendation that ``tools.auto_paper.pipeline.place_candidate``
threads through to broker placement.

This module mirrors :func:`tools.thematic_portfolio.orchestrator.aggregate_critic_outputs`
but adapted to swing trading: instead of ``weight_reduction_applied`` against
a target percentage, we emit a ``sizing_multiplier`` in [0.0, 1.0] applied
to ``CandidateInput.shares``. The same 4-value confidence_adjustment
vocabulary is preserved across both panels for consistency.

Priority rules (highest-priority first; a higher-priority rule short-circuits):

1. ANY ``structural_risk`` → ``action="defer"``, ``sizing_multiplier=0.0``.
   The candidate is NOT placed today; surfaces for manual review tomorrow.
2. ANY ``minus_50`` → ``action="half_size_review"``, ``sizing_multiplier=0.5``.
   Place at half size and flag for review.
3. ≥ 2 critics output ``minus_20`` → ``action="reduce_20"``,
   ``sizing_multiplier=0.8``. Place at 80% size.
4. Otherwise → ``action="preserve"``, ``sizing_multiplier=1.0``. Place at
   full size; log concerns for the record.

Shadow mode (default ``True`` until 2026-06-10): the aggregator still
computes ``sizing_multiplier``, but ``pipeline.place_candidate`` ignores it
when ``apply_panel_sizing=False``. The verdict surfaces in summaries + the
calibration log either way.

CLI::

    uv run python -m tools.auto_paper.critic_panel \\
        --ticker VRT --votes path/to/vote1.json --votes path/to/vote2.json ...
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

TOOL = "tools/auto_paper/critic_panel.py"

_ROOT = Path(__file__).resolve().parents[2]
_PANEL_LEDGER_DIR = _ROOT / "ledgers" / "swing-critics"
_CALIBRATION_DIR = _PANEL_LEDGER_DIR / "_calibration"

# Valid confidence-adjustment vocabulary. Shared with the thematic-critic
# panel (`tools.thematic_portfolio.orchestrator.VALID_CONFIDENCE_ADJUSTMENTS`)
# — keep in sync if either side adds a value.
VALID_CONFIDENCE_ADJUSTMENTS = frozenset(
    ["hold", "minus_20", "minus_50", "structural_risk"]
)

# Valid action vocabulary. Specific to the swing panel (the thematic panel
# uses {preserve, trim, hold_pending_bertrand_review} which maps to different
# downstream consumers).
VALID_PANEL_ACTIONS = frozenset(
    ["preserve", "reduce_20", "half_size_review", "defer"]
)

# Sizing-multiplier table — single source of truth.
_ACTION_TO_MULTIPLIER: dict[str, float] = {
    "preserve": 1.0,
    "reduce_20": 0.8,
    "half_size_review": 0.5,
    "defer": 0.0,
}


# ---------------------------------------------------------------------------
# Vote + verdict dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CriticVote:
    """One critic's vote on a candidate. Mirrors the JSON output shape
    defined in `.claude/agents/swing-critics/_template.md`."""

    critic: str                      # e.g. "risk_manager", "patel"
    candidate_ticker: str
    panel_call_id: str
    panel_firing_date: str           # ISO date
    risks: list[dict[str, Any]]      # [{risk, grounding_evidence, severity}]
    confidence_adjustment: str       # ∈ VALID_CONFIDENCE_ADJUSTMENTS
    adjustment_rationale: str
    estimated_cost_usd: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CriticVote":
        # Accept either `grounding_evidence` (swing critics) or
        # `grounding_citation` (reused thematic critics) in risks[].
        # Normalize on read — we copy whichever is present into both fields
        # so downstream consumers can index by either.
        risks = []
        for r in d.get("risks", []):
            risk_normalized = dict(r)
            if "grounding_evidence" not in risk_normalized and "grounding_citation" in risk_normalized:
                risk_normalized["grounding_evidence"] = risk_normalized["grounding_citation"]
            elif "grounding_citation" not in risk_normalized and "grounding_evidence" in risk_normalized:
                risk_normalized["grounding_citation"] = risk_normalized["grounding_evidence"]
            risks.append(risk_normalized)
        return cls(
            critic=d["critic"],
            candidate_ticker=d.get("candidate_ticker") or d.get("position_ticker"),
            panel_call_id=d.get("panel_call_id") or d.get("critic_call_id", ""),
            panel_firing_date=d.get("panel_firing_date") or "",
            risks=risks,
            confidence_adjustment=d["confidence_adjustment"],
            adjustment_rationale=d.get("adjustment_rationale", ""),
            estimated_cost_usd=float(d.get("estimated_cost_usd", 0.0)),
        )


@dataclass
class PanelVerdict:
    """Aggregated panel result for one candidate."""

    ticker: str
    action: str                                 # ∈ VALID_PANEL_ACTIONS
    sizing_multiplier: float                    # ∈ {0.0, 0.5, 0.8, 1.0}
    n_critics_total: int
    n_critics_hold: int
    n_critics_minus_20: int
    n_critics_minus_50: int
    n_critics_structural_risk: int
    structural_risk_critics: list[str]
    minus_50_critics: list[str]
    minus_20_critics: list[str]
    rationale: str
    total_cost_usd: float
    shadow_mode: bool
    computed_at: str
    panel_call_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def aggregate_panel(
    votes: list[CriticVote],
    *,
    ticker: str,
    panel_call_id: str,
    shadow_mode: bool = True,
) -> PanelVerdict:
    """Apply the priority rules; return :class:`PanelVerdict`.

    Args:
        votes: list of :class:`CriticVote` from N critics.
        ticker: candidate ticker.
        panel_call_id: unique id for this panel firing (e.g.
            ``"2026-05-27T22-15__VRT"``).
        shadow_mode: when True, the verdict still computes ``sizing_multiplier``
            but downstream consumers (``pipeline.place_candidate``) ignore it.
            Logged so the operator can see the toggle's state.

    Returns:
        :class:`PanelVerdict`.

    Raises:
        ValueError: any vote has an invalid ``confidence_adjustment``.
        ValueError: empty votes list (no critics ran — caller should handle
            this case BEFORE calling aggregate_panel; a zero-input panel
            verdict would be misleading).
    """
    if not votes:
        raise ValueError("aggregate_panel requires at least one CriticVote")

    structural_risk: list[str] = []
    minus_50: list[str] = []
    minus_20: list[str] = []
    holds: list[str] = []
    total_cost = 0.0
    for v in votes:
        adj = v.confidence_adjustment
        if adj not in VALID_CONFIDENCE_ADJUSTMENTS:
            raise ValueError(
                f"critic {v.critic!r} returned invalid confidence_adjustment "
                f"{adj!r}; must be one of {sorted(VALID_CONFIDENCE_ADJUSTMENTS)}"
            )
        if adj == "structural_risk":
            structural_risk.append(v.critic)
        elif adj == "minus_50":
            minus_50.append(v.critic)
        elif adj == "minus_20":
            minus_20.append(v.critic)
        else:
            holds.append(v.critic)
        total_cost += v.estimated_cost_usd

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    base = dict(
        ticker=ticker,
        n_critics_total=len(votes),
        n_critics_hold=len(holds),
        n_critics_minus_20=len(minus_20),
        n_critics_minus_50=len(minus_50),
        n_critics_structural_risk=len(structural_risk),
        structural_risk_critics=sorted(structural_risk),
        minus_50_critics=sorted(minus_50),
        minus_20_critics=sorted(minus_20),
        total_cost_usd=round(total_cost, 4),
        shadow_mode=shadow_mode,
        computed_at=now,
        panel_call_id=panel_call_id,
    )

    # Rule 1 — any structural_risk → defer (highest priority).
    if structural_risk:
        return PanelVerdict(
            action="defer",
            sizing_multiplier=_ACTION_TO_MULTIPLIER["defer"],
            rationale=(
                f"STRUCTURAL_RISK fired by {sorted(structural_risk)}; defer "
                f"placement for manual review."
            ),
            **base,
        )

    # Rule 2 — any minus_50 → half-size + review.
    if minus_50:
        return PanelVerdict(
            action="half_size_review",
            sizing_multiplier=_ACTION_TO_MULTIPLIER["half_size_review"],
            rationale=(
                f"minus_50 from {sorted(minus_50)}; place at half size and "
                f"flag for manual review."
            ),
            **base,
        )

    # Rule 3 — ≥ 2 critics at minus_20 → reduce 20%.
    if len(minus_20) >= 2:
        return PanelVerdict(
            action="reduce_20",
            sizing_multiplier=_ACTION_TO_MULTIPLIER["reduce_20"],
            rationale=(
                f"{len(minus_20)} critics ({sorted(minus_20)}) recommend "
                f"minus_20; apply 20% size reduction."
            ),
            **base,
        )

    # Rule 4 — preserve.
    rationale_bits = [
        f"{len(votes)} critics ran; {len(holds)} hold, {len(minus_20)} minus_20"
    ]
    if minus_20:
        rationale_bits.append(
            f"single minus_20 from {minus_20[0]} (logged, below ≥2 threshold)"
        )
    return PanelVerdict(
        action="preserve",
        sizing_multiplier=_ACTION_TO_MULTIPLIER["preserve"],
        rationale="; ".join(rationale_bits) + "; full size.",
        **base,
    )


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def save_critic_vote(
    vote: CriticVote,
    *,
    ledger_date: date | None = None,
    panel_dir: Path | None = None,
) -> Path:
    """Write one critic's vote to
    ``ledgers/swing-critics/YYYY-MM-DD/<TICKER>/<CRITIC>.json``."""
    if ledger_date is None:
        ledger_date = date.today()
    if panel_dir is None:
        panel_dir = _PANEL_LEDGER_DIR
    day_dir = panel_dir / ledger_date.isoformat() / vote.candidate_ticker
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{vote.critic}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(vote.to_dict(), fh, indent=2)
    return path


def save_panel_verdict(
    verdict: PanelVerdict,
    *,
    ledger_date: date | None = None,
    panel_dir: Path | None = None,
) -> Path:
    """Write the aggregated verdict to
    ``ledgers/swing-critics/YYYY-MM-DD/<TICKER>/_panel.json``."""
    if ledger_date is None:
        ledger_date = date.today()
    if panel_dir is None:
        panel_dir = _PANEL_LEDGER_DIR
    day_dir = panel_dir / ledger_date.isoformat() / verdict.ticker
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "_panel.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(verdict.to_dict(), fh, indent=2)
    return path


def append_calibration_log(
    verdict: PanelVerdict,
    *,
    placement_status: str,
    placement_shares: Optional[int] = None,
    ledger_date: date | None = None,
    panel_dir: Path | None = None,
) -> Path:
    """Append a panel-verdict + placement-outcome line to
    ``ledgers/swing-critics/_calibration/YYYY-MM-DD.jsonl`` (one JSON per line).

    Used by ``/auto-paper-perf`` to compute realized P&L vs panel verdict
    after positions close. The append-only format means each day's file
    accumulates all candidates scored that day.

    Args:
        placement_status: outcome from PlacementResult (placed / rejected /
            dry_run / error).
        placement_shares: actual shares placed if status == placed.
    """
    if ledger_date is None:
        ledger_date = date.today()
    if panel_dir is None:
        panel_dir = _PANEL_LEDGER_DIR
    cal_dir = panel_dir / "_calibration"
    cal_dir.mkdir(parents=True, exist_ok=True)
    path = cal_dir / f"{ledger_date.isoformat()}.jsonl"
    entry = {
        "ticker": verdict.ticker,
        "action": verdict.action,
        "sizing_multiplier": verdict.sizing_multiplier,
        "n_critics_total": verdict.n_critics_total,
        "n_minus_20": verdict.n_critics_minus_20,
        "n_minus_50": verdict.n_critics_minus_50,
        "n_structural_risk": verdict.n_critics_structural_risk,
        "shadow_mode": verdict.shadow_mode,
        "computed_at": verdict.computed_at,
        "panel_call_id": verdict.panel_call_id,
        "placement_status": placement_status,
        "placement_shares": placement_shares,
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.auto_paper.critic_panel",
        description=(
            "Aggregate N critic-vote JSON files into a panel verdict. "
            "Used for offline replay; production calls the Python API directly."
        ),
    )
    p.add_argument("--ticker", required=True)
    p.add_argument(
        "--vote", action="append", required=True, dest="vote_paths",
        help="Path to a critic-vote JSON file. Pass multiple --vote flags "
             "(one per critic) to aggregate.",
    )
    p.add_argument(
        "--panel-call-id", default=None,
        help="Unique id for this panel firing. Defaults to "
             "<utcnow-iso>__<ticker>.",
    )
    p.add_argument(
        "--live", action="store_true",
        help="Set shadow_mode=False (panel verdict will be APPLIED by the "
             "pipeline). Default is shadow mode.",
    )
    p.add_argument(
        "--write", action="store_true",
        help="Persist the verdict to ledgers/swing-critics/...",
    )
    args = p.parse_args()

    votes = []
    for vp in args.vote_paths:
        with open(vp, encoding="utf-8") as fh:
            votes.append(CriticVote.from_dict(json.load(fh)))

    panel_call_id = args.panel_call_id or (
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M')}__{args.ticker}"
    )
    verdict = aggregate_panel(
        votes, ticker=args.ticker, panel_call_id=panel_call_id,
        shadow_mode=not args.live,
    )

    if args.write:
        save_panel_verdict(verdict)
        for v in votes:
            save_critic_vote(v)

    print(json.dumps(verdict.to_dict(), indent=2))


if __name__ == "__main__":
    main()
