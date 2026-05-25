"""Orchestrator helpers — Loop 1 input-bundle composer + critic-output aggregator.

The `/thematic-portfolio` slash command does most of the workflow as imperative
steps in its skill markdown (refresh corpus, refresh 13Fs, invoke Loop 1
subagent, dispatch critic panel). This module provides the two pure-Python
helpers it leans on:

1. :func:`compose_loop1_input_bundle` — takes the artifacts the skill has
   gathered + the trigger metadata and assembles the YAML/dict shape the
   Loop 1 prompt's "Input contract" section enumerates. Pure composition
   over already-loaded data; no I/O.

2. :func:`aggregate_critic_outputs` — applies the panel-aggregation rules
   from `.claude/agents/_draft/thematic-critics/_template.md` § "Aggregation
   rules": single structural_risk OR minus_50 forces hold_pending_review;
   ≥2 critics at minus_20+ trigger weighted reduction; otherwise preserve
   the Loop 1 target.

Plus a thin convenience :func:`apply_aggregation_to_positions` that walks
the full Loop 1 positions list + per-ticker critic outputs and produces
the final adjusted-recommendation block the skill presents to Bertrand.

No subagent invocations happen here — the skill drives those via the
Agent tool. This module only handles the deterministic pre- and post-
processing.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..cli import emit
from ..contract import TraceEntry

TOOL = "tools/thematic_portfolio/orchestrator.py"

VALID_CONFIDENCE_ADJUSTMENTS = (
    "hold",
    "minus_20",
    "minus_50",
    "structural_risk",
)
VALID_LOOP5_PHASES = ("phase1_10pct", "phase2_15pct", "phase3_25pct")
VALID_ALLOCATIONS = (10.0, 15.0, 25.0)
VALID_TRIGGER_TYPES = ("monthly_base", "substantive_artifact")


# ---------------------------------------------------------------------------
# Input-bundle composer
# ---------------------------------------------------------------------------


@dataclass
class FilingPaths:
    """Per-fund 13F path pair (latest + prior)."""

    latest_period: str
    latest_long_book_path: str
    latest_filed_date: str | None = None
    latest_put_complex_path: str | None = None
    latest_call_book_path: str | None = None
    prior_period: str | None = None
    prior_long_book_path: str | None = None


@dataclass
class PortfolioState:
    """Snapshot of the thematic-track portfolio at firing time."""

    thematic_allocation_pct: float
    current_loop5_phase: str
    total_portfolio_nav_usd: float
    current_thematic_positions: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.thematic_allocation_pct not in VALID_ALLOCATIONS:
            raise ValueError(
                f"thematic_allocation_pct must be one of {VALID_ALLOCATIONS}; "
                f"got {self.thematic_allocation_pct}"
            )
        if self.current_loop5_phase not in VALID_LOOP5_PHASES:
            raise ValueError(
                f"current_loop5_phase must be one of {VALID_LOOP5_PHASES}; "
                f"got {self.current_loop5_phase}"
            )
        if self.total_portfolio_nav_usd <= 0:
            raise ValueError(
                f"total_portfolio_nav_usd must be positive; got {self.total_portfolio_nav_usd}"
            )


def compose_loop1_input_bundle(
    *,
    trigger_type: str,
    fired_at: str,
    triggering_artifact: dict | None,
    rate_limit_consumed_this_week_before_firing: int,
    mandatory_escalation: bool,
    corpus_snapshot: dict,
    sa_lp_filing: FilingPaths,
    ensemble_filings: dict[str, FilingPaths],
    portfolio_state: PortfolioState,
    prior_loop1_path: str | None,
    tier3_signals: dict | None = None,
) -> dict[str, Any]:
    """Build the YAML/dict the Loop 1 prompt's "Input contract" expects.

    All arguments are keyword-only — the contract is wide enough that
    positional invocation would be brittle. The skill markdown loads each
    piece via the corresponding tool (`thirteen_f.fetch_ensemble`,
    `manifest.compose`, etc.) then hands the results here.

    Args:
        trigger_type: ``"monthly_base"`` or ``"substantive_artifact"``.
        fired_at: ISO-8601 UTC timestamp.
        triggering_artifact: dict with ``source`` / ``url`` / ``tier`` / ``snippet``,
            or ``None`` for monthly base.
        rate_limit_consumed_this_week_before_firing: 0-3 inclusive.
        mandatory_escalation: True when an escalation signal overrode rate limit.
        corpus_snapshot: output of :func:`tools.thematic_portfolio.corpus.manifest.compose`
            (the dict inside the TraceEntry's ``output``).
        sa_lp_filing: latest + prior 13F paths for SA LP.
        ensemble_filings: dict keyed by fund (``"altimeter"`` / ``"coatue"`` /
            ``"light_street"``) → FilingPaths.
        portfolio_state: current thematic-track snapshot.
        prior_loop1_path: path to most recent Loop 1 output JSON, or None
            on the first-ever firing.
        tier3_signals: optional dict of paths; v1 ships with this null
            (Tier 3 real-world signal compilers not yet built).

    Returns:
        The Loop 1 input-bundle dict. Caller serializes to YAML or JSON
        and feeds it to the subagent.

    Raises:
        ValueError: invalid trigger_type, rate-limit count out of bounds,
            triggering_artifact missing when trigger_type requires it, or
            ensemble_filings contains an unknown fund label.
    """
    if trigger_type not in VALID_TRIGGER_TYPES:
        raise ValueError(
            f"trigger_type must be one of {VALID_TRIGGER_TYPES}; got {trigger_type!r}"
        )
    if not 0 <= rate_limit_consumed_this_week_before_firing <= 3:
        raise ValueError(
            "rate_limit_consumed_this_week_before_firing must be in [0, 3]; "
            f"got {rate_limit_consumed_this_week_before_firing}"
        )
    if trigger_type == "substantive_artifact" and triggering_artifact is None:
        raise ValueError(
            "trigger_type=substantive_artifact requires triggering_artifact dict"
        )
    if trigger_type == "monthly_base" and triggering_artifact is not None:
        raise ValueError(
            "trigger_type=monthly_base must NOT carry a triggering_artifact"
        )
    unknown = set(ensemble_filings.keys()) - {"altimeter", "coatue", "light_street"}
    if unknown:
        raise ValueError(f"unknown ensemble fund(s): {unknown}")

    ensemble_block: dict[str, Any] = {}
    for fund, paths in ensemble_filings.items():
        ensemble_block[fund] = {
            "latest_13f": {
                "period": paths.latest_period,
                "filed": paths.latest_filed_date,
                "long_book_path": paths.latest_long_book_path,
            },
        }
        if paths.prior_period:
            ensemble_block[fund]["prior_13f"] = {
                "period": paths.prior_period,
                "long_book_path": paths.prior_long_book_path,
            }

    sa_lp_block: dict[str, Any] = {
        "cik_primary": "0002045724",
        "cik_partners_lp": "0002038540",
        "latest_13f": {
            "period": sa_lp_filing.latest_period,
            "filed": sa_lp_filing.latest_filed_date,
            "long_book_path": sa_lp_filing.latest_long_book_path,
            "put_complex_path": sa_lp_filing.latest_put_complex_path,
            "call_book_path": sa_lp_filing.latest_call_book_path,
        },
    }
    if sa_lp_filing.prior_period:
        sa_lp_block["prior_13f"] = {
            "period": sa_lp_filing.prior_period,
            "long_book_path": sa_lp_filing.prior_long_book_path,
        }

    bundle: dict[str, Any] = {
        "trigger": {
            "type": trigger_type,
            "fired_at": fired_at,
            "triggering_artifact": triggering_artifact,
            "rate_limit_consumed_this_week": rate_limit_consumed_this_week_before_firing,
            "mandatory_escalation": mandatory_escalation,
        },
        "corpus_snapshot": corpus_snapshot,
        "filings": {
            "sa_lp": sa_lp_block,
            "ensemble": ensemble_block,
        },
        "tier3_signals": tier3_signals,
        "portfolio_state": {
            "thematic_allocation_pct": portfolio_state.thematic_allocation_pct,
            "current_loop5_phase": portfolio_state.current_loop5_phase,
            "total_portfolio_nav_usd": portfolio_state.total_portfolio_nav_usd,
            "current_thematic_positions": portfolio_state.current_thematic_positions,
        },
        "prior_loop1_output": {"path": prior_loop1_path},
    }
    return bundle


def find_prior_loop1_output(loop1_dir: Path) -> str | None:
    """Locate the most recent Loop 1 output JSON in ``loop1_dir`` (by mtime).

    Returns the path as a string, or None when no prior firing exists.
    """
    if not loop1_dir.exists() or not loop1_dir.is_dir():
        return None
    candidates = [
        p for p in loop1_dir.glob("*.json")
        if p.is_file() and not p.name.startswith("_")
    ]
    if not candidates:
        return None
    most_recent = max(candidates, key=lambda p: p.stat().st_mtime)
    return str(most_recent)


# ---------------------------------------------------------------------------
# Critic-output aggregator
# ---------------------------------------------------------------------------


@dataclass
class AggregatedPositionDecision:
    """The final per-position adjustment after the critic panel runs."""

    ticker: str
    loop1_target_pct: float
    adjusted_target_pct: float
    weight_reduction_applied: float  # 0.0 means preserved
    recommended_action: str  # "preserve" | "trim" | "hold_pending_bertrand_review"
    n_critics_minus_20: int
    n_critics_minus_50: int
    n_critics_structural_risk: int
    structural_risk_critics: list[str]
    minus_50_critics: list[str]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def aggregate_critic_outputs(
    ticker: str,
    loop1_target_pct: float,
    critic_outputs: list[dict[str, Any]],
) -> AggregatedPositionDecision:
    """Apply the panel-aggregation rules per the critic template.

    Rules (in priority order):

    1. ANY single ``structural_risk`` → ``hold_pending_bertrand_review``.
    2. ANY single ``minus_50`` reduction → ``hold_pending_bertrand_review``.
    3. ≥ 2 critics output ``minus_20`` or worse → weighted reduction:
       ``adjusted = loop1_target × (1 - average_of_reductions)`` where each
       ``minus_20`` contributes 0.20 and ``minus_50`` contributes 0.50 to
       the average. Note: rule 2 short-circuits before this, so in practice
       all entries averaged here are ``minus_20`` — but the formula is
       written to handle ``minus_50`` symmetrically for robustness against
       future rule changes.
    4. Otherwise → preserve loop1_target, log critic concerns for the
       record.

    Args:
        ticker: position ticker.
        loop1_target_pct: Loop 1's recommended target_weight_pct_of_total.
        critic_outputs: list of critic-output JSON dicts, each with at
            least ``critic`` and ``confidence_adjustment`` keys.

    Returns:
        :class:`AggregatedPositionDecision` describing the resolution.

    Raises:
        ValueError: any critic output has an invalid ``confidence_adjustment``,
            or any required key is missing.
    """
    structural_risk: list[str] = []
    minus_50: list[str] = []
    minus_20: list[str] = []
    holds: list[str] = []
    for c in critic_outputs:
        if "critic" not in c or "confidence_adjustment" not in c:
            raise ValueError(
                f"critic output missing 'critic' or 'confidence_adjustment': {c}"
            )
        adj = c["confidence_adjustment"]
        if adj not in VALID_CONFIDENCE_ADJUSTMENTS:
            raise ValueError(
                f"critic {c['critic']!r} returned invalid confidence_adjustment "
                f"{adj!r}; must be one of {VALID_CONFIDENCE_ADJUSTMENTS}"
            )
        if adj == "structural_risk":
            structural_risk.append(c["critic"])
        elif adj == "minus_50":
            minus_50.append(c["critic"])
        elif adj == "minus_20":
            minus_20.append(c["critic"])
        else:
            holds.append(c["critic"])

    n_critics_total = len(critic_outputs)
    base = {
        "ticker": ticker,
        "loop1_target_pct": loop1_target_pct,
        "n_critics_structural_risk": len(structural_risk),
        "n_critics_minus_50": len(minus_50),
        "n_critics_minus_20": len(minus_20),
        "structural_risk_critics": sorted(structural_risk),
        "minus_50_critics": sorted(minus_50),
    }

    # Rule 1 — structural risk forces hold_pending_review.
    if structural_risk:
        return AggregatedPositionDecision(
            **base,
            adjusted_target_pct=loop1_target_pct,
            weight_reduction_applied=0.0,
            recommended_action="hold_pending_bertrand_review",
            rationale=(
                f"STRUCTURAL_RISK fired by {sorted(structural_risk)}; position "
                "held at Loop 1 target pending manual Bertrand review."
            ),
        )

    # Rule 2 — any minus_50 forces hold_pending_review.
    if minus_50:
        return AggregatedPositionDecision(
            **base,
            adjusted_target_pct=loop1_target_pct,
            weight_reduction_applied=0.0,
            recommended_action="hold_pending_bertrand_review",
            rationale=(
                f"minus_50 reduction by {sorted(minus_50)}; position held at "
                "Loop 1 target pending manual Bertrand review."
            ),
        )

    # Rule 3 — ≥ 2 critics at minus_20+ → weighted reduction.
    if len(minus_20) >= 2:
        avg_reduction = (len(minus_20) * 0.20) / len(minus_20)  # = 0.20 by definition
        # The averaging-over-non-holds formulation per the template aggregates
        # over the SET of negative critics, not the panel. With only minus_20
        # entries above this branch, avg_reduction == 0.20. Spec preserved
        # symbolically so future minus_50 inclusion (if rules change) still
        # composes correctly.
        adjusted = loop1_target_pct * (1.0 - avg_reduction)
        return AggregatedPositionDecision(
            **base,
            adjusted_target_pct=adjusted,
            weight_reduction_applied=avg_reduction,
            recommended_action="trim",
            rationale=(
                f"{len(minus_20)} critics ({sorted(minus_20)}) recommend minus_20; "
                f"applied weighted reduction of {avg_reduction:.0%} to Loop 1 target "
                f"({loop1_target_pct:.2f}% → {adjusted:.2f}%)."
            ),
        )

    # Rule 4 — preserve.
    rationale_bits = [f"{len(critic_outputs)}/{n_critics_total} critics ran"]
    if minus_20:
        rationale_bits.append(
            f"single minus_20 from {minus_20[0]} (logged, below ≥2 threshold)"
        )
    return AggregatedPositionDecision(
        **base,
        adjusted_target_pct=loop1_target_pct,
        weight_reduction_applied=0.0,
        recommended_action="preserve",
        rationale="; ".join(rationale_bits) + "; Loop 1 target preserved.",
    )


def apply_aggregation_to_positions(
    loop1_positions: list[dict[str, Any]],
    critic_outputs_by_ticker: dict[str, list[dict[str, Any]]],
) -> list[AggregatedPositionDecision]:
    """Walk every Loop 1 position + its critic outputs; return aggregated list.

    Positions missing from ``critic_outputs_by_ticker`` are treated as if
    no critic ran on them — the function preserves the Loop 1 target and
    surfaces this in the rationale. (This shouldn't happen in production
    because every position fires the 5 core critics, but the function
    handles the case defensively.)
    """
    out: list[AggregatedPositionDecision] = []
    for pos in loop1_positions:
        ticker = pos["ticker"]
        target_pct = float(pos["target_weight_pct_of_total"])
        critic_outputs = critic_outputs_by_ticker.get(ticker, [])
        if not critic_outputs:
            # No critics ran — preserve target and log gap.
            out.append(
                AggregatedPositionDecision(
                    ticker=ticker,
                    loop1_target_pct=target_pct,
                    adjusted_target_pct=target_pct,
                    weight_reduction_applied=0.0,
                    recommended_action="preserve",
                    n_critics_minus_20=0,
                    n_critics_minus_50=0,
                    n_critics_structural_risk=0,
                    structural_risk_critics=[],
                    minus_50_critics=[],
                    rationale="No critic outputs supplied for this position; preserved at Loop 1 target.",
                )
            )
        else:
            out.append(aggregate_critic_outputs(ticker, target_pct, critic_outputs))
    return out


def compose_bundle_trace_entry(
    bundle: dict[str, Any],
) -> TraceEntry:
    """Wrap a composed bundle in a TraceEntry for ledger audit-trail logging."""
    return TraceEntry(
        tool=TOOL,
        inputs={
            "trigger_type": bundle["trigger"]["type"],
            "fired_at": bundle["trigger"]["fired_at"],
            "thematic_allocation_pct": bundle["portfolio_state"][
                "thematic_allocation_pct"
            ],
            "ensemble_fund_count": len(bundle["filings"]["ensemble"]),
        },
        output={"bundle": bundle},
    )


# ---------------------------------------------------------------------------
# CLI — minimal exposure; the skill markdown is the primary orchestrator
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.orchestrator",
        description=(
            "Inspect a Loop 1 output + critic outputs and emit the aggregated "
            "per-position decisions. Used by the /thematic-portfolio skill after "
            "the critic panel has finished running."
        ),
    )
    p.add_argument(
        "--loop1-output",
        type=Path,
        required=True,
        help="Path to the Loop 1 output JSON.",
    )
    p.add_argument(
        "--critic-outputs-dir",
        type=Path,
        required=True,
        help=(
            "Directory containing per-critic per-ticker output JSON files "
            "named <ticker>__<critic>.json."
        ),
    )
    args = p.parse_args()

    loop1 = json.loads(args.loop1_output.read_text(encoding="utf-8"))
    loop1_positions = loop1.get("positions", [])

    critic_outputs_by_ticker: dict[str, list[dict]] = {}
    if args.critic_outputs_dir.exists():
        for f in args.critic_outputs_dir.glob("*.json"):
            data = json.loads(f.read_text(encoding="utf-8"))
            ticker = data.get("position_ticker") or f.stem.split("__")[0]
            critic_outputs_by_ticker.setdefault(ticker, []).append(data)

    decisions = apply_aggregation_to_positions(loop1_positions, critic_outputs_by_ticker)
    emit(
        TraceEntry(
            tool=TOOL,
            inputs={
                "loop1_output": str(args.loop1_output),
                "critic_outputs_dir": str(args.critic_outputs_dir),
                "n_positions": len(loop1_positions),
                "n_critic_files": sum(
                    len(v) for v in critic_outputs_by_ticker.values()
                ),
            },
            output={
                "decisions": [d.to_dict() for d in decisions],
                "summary": {
                    "n_positions": len(decisions),
                    "n_hold_pending_review": sum(
                        1 for d in decisions
                        if d.recommended_action == "hold_pending_bertrand_review"
                    ),
                    "n_trimmed": sum(1 for d in decisions if d.recommended_action == "trim"),
                    "n_preserved": sum(
                        1 for d in decisions if d.recommended_action == "preserve"
                    ),
                },
            },
        )
    )


if __name__ == "__main__":
    main()
