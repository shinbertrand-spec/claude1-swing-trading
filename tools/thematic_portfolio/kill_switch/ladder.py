"""3-tier CPPI deleveraging ladder — pure deterministic compute().

Per [[swing-thematic-portfolio-kill-switch-architecture]] § Process B's
deterministic logic:

    if drawdown >= 0.50 or aschenbrenner_kill_event_detected():
        execute_tier_3_unwind()
    elif drawdown >= 0.35 and current_allocation_pct > 12.5:
        execute_tier_2_deleverage()
    elif drawdown >= 0.20 and current_allocation_pct > 17.5:
        execute_tier_1_deleverage()

This module is pure: no I/O, no network, no LLM calls. It takes a
:class:`KillSwitchInputs` and returns a :class:`KillSwitchDecision`.
The monitor loop is responsible for fetching the inputs (from broker
+ peak state file + kill-event flag file) and for acting on the decision.

## Sell-fraction semantics

The decision returns ``sell_fraction`` = how much of the current thematic
book to sell to bring the allocation down to the tier's target:

    sell_fraction = 1 - target_allocation_pct / current_allocation_pct

(Clamped to [0.0, 1.0].) The monitor applies this fraction to per-position
share counts when generating sell orders.

* ``Tier 1`` with current_allocation_pct = 25% and target = 17.5%:
  sell_fraction = 1 - 17.5/25 = 0.30 (sell 30% of each thematic position).
* ``Tier 3`` always sets sell_fraction = 1.0 (full unwind).

## Idempotency

The ladder is stateless. Tier-already-fired suppression is handled by
the monitor: if ``previous_fired_tier`` is provided in the inputs, the
ladder will not propose a tier lower than (or equal to) what already
fired (because the resulting target_allocation would not reduce
exposure further).

Repeated firing of the same tier is naturally suppressed by the
``current_allocation_pct > target_allocation_pct`` guard: once we have
already sold down to or below the target, ``sell_fraction <= 0`` and the
decision is ``action=hold`` with rationale "already at or below target".
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from ...contract import TraceEntry

TOOL = "tools/thematic_portfolio/kill_switch/ladder.py"

# Drawdown thresholds (fraction of peak). Edit in lockstep with the design
# memo [[swing-thematic-portfolio-kill-switch-architecture]] Q10 table.
TIER_1_DD_THRESHOLD = 0.20
TIER_2_DD_THRESHOLD = 0.35
TIER_3_DD_THRESHOLD = 0.50

# Target allocations per tier (fraction of total account net liquidation).
# The reasoning: from a 25% baseline, tier_1 reduces 30% of the book
# (25 -> 17.5), tier_2 reduces a further 30% of the remaining (17.5 -> 12.5),
# tier_3 unwinds entirely.
TIER_1_TARGET_ALLOCATION = 0.175
TIER_2_TARGET_ALLOCATION = 0.125
TIER_3_TARGET_ALLOCATION = 0.0


@dataclass(frozen=True)
class KillSwitchInputs:
    """Inputs to the ladder compute().

    Attributes:
        thematic_market_value: sum of current market value across all
            thematic-tagged Tiger positions (USD).
        peak_thematic_value: rolling peak of ``thematic_market_value``
            observed since the kill-switch began monitoring (or since
            the last full reset). Set equal to ``thematic_market_value``
            on the first cycle.
        total_account_value: total account net liquidation across ALL
            books (thematic + paper-auto + human-discretionary + cash).
            Used to compute ``current_allocation_pct``.
        aschenbrenner_kill_event: boolean flag set externally by the
            artifact classifier on thesis-abandonment / SA LP closure
            signals. When True, fires Tier 3 regardless of drawdown.
        previous_fired_tier: tier number (0/1/2/3) of the most recent
            fired event in the event log, or 0 if none. Used for
            informational rationale only — the ladder's tier selection
            depends on current state, not history.
    """

    thematic_market_value: float
    peak_thematic_value: float
    total_account_value: float
    aschenbrenner_kill_event: bool = False
    previous_fired_tier: int = 0


@dataclass
class KillSwitchDecision:
    """Output of the ladder compute()."""

    action: str  # "hold" | "deleverage" | "unwind"
    tier: int  # 0 = hold, 1/2/3 = deleverage tier
    drawdown_pct: float  # 0.0 - 1.0
    current_allocation_pct: float  # 0.0 - 1.0
    target_allocation_pct: float  # 0.0 - 1.0
    sell_fraction: float  # 0.0 - 1.0 (fraction of thematic book to sell)
    rationale: str
    aschenbrenner_override: bool = False  # tier-3 fired via kill-event flag
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _drawdown(current: float, peak: float) -> float:
    if peak <= 0:
        return 0.0
    return max(0.0, (peak - current) / peak)


def _allocation_pct(thematic_value: float, total_account_value: float) -> float:
    if total_account_value <= 0:
        return 0.0
    return thematic_value / total_account_value


def _hold(
    drawdown: float,
    alloc: float,
    rationale: str,
    warnings: Optional[list[str]] = None,
) -> KillSwitchDecision:
    return KillSwitchDecision(
        action="hold",
        tier=0,
        drawdown_pct=drawdown,
        current_allocation_pct=alloc,
        target_allocation_pct=alloc,  # no change
        sell_fraction=0.0,
        rationale=rationale,
        warnings=list(warnings or []),
    )


def _deleverage(
    tier: int,
    drawdown: float,
    alloc: float,
    target_alloc: float,
    rationale: str,
    aschenbrenner_override: bool = False,
    warnings: Optional[list[str]] = None,
) -> KillSwitchDecision:
    sell_fraction = 0.0
    if alloc > 0:
        sell_fraction = max(0.0, min(1.0, 1.0 - target_alloc / alloc))
    action = "unwind" if tier == 3 else "deleverage"
    return KillSwitchDecision(
        action=action,
        tier=tier,
        drawdown_pct=drawdown,
        current_allocation_pct=alloc,
        target_allocation_pct=target_alloc,
        sell_fraction=sell_fraction,
        rationale=rationale,
        aschenbrenner_override=aschenbrenner_override,
        warnings=list(warnings or []),
    )


def compute(inputs: KillSwitchInputs) -> KillSwitchDecision:
    """Pure deterministic 3-tier CPPI ladder.

    Selection order (highest tier wins):
    1. Aschenbrenner-kill-event flag -> tier 3 (regardless of drawdown).
    2. Drawdown >= 50% -> tier 3.
    3. Drawdown >= 35% AND current_allocation_pct > target_2 -> tier 2.
    4. Drawdown >= 20% AND current_allocation_pct > target_1 -> tier 1.
    5. Otherwise -> hold.

    The ``> target`` guards prevent redundant fires once we have already
    sold down to or below the tier's target. They are STRICT inequality
    so that we don't keep re-firing the same tier.
    """
    warnings: list[str] = []
    if inputs.peak_thematic_value < inputs.thematic_market_value:
        warnings.append(
            "peak_thematic_value < thematic_market_value — caller should "
            "have updated the peak before invoking compute(); using "
            "thematic_market_value as effective peak."
        )
        effective_peak = inputs.thematic_market_value
    else:
        effective_peak = inputs.peak_thematic_value

    drawdown = _drawdown(inputs.thematic_market_value, effective_peak)
    alloc = _allocation_pct(
        inputs.thematic_market_value, inputs.total_account_value
    )

    # 1. Aschenbrenner-kill-event flag is sovereign over the ladder.
    if inputs.aschenbrenner_kill_event:
        if alloc <= TIER_3_TARGET_ALLOCATION:
            return _hold(
                drawdown,
                alloc,
                "aschenbrenner_kill_event=True but already fully unwound "
                "(allocation <= 0). No further action.",
                warnings=warnings,
            )
        return _deleverage(
            tier=3,
            drawdown=drawdown,
            alloc=alloc,
            target_alloc=TIER_3_TARGET_ALLOCATION,
            rationale=(
                "aschenbrenner_kill_event=True (thesis-abandonment / "
                "SA LP closure / regulatory action / principal incident). "
                "Tier 3 full-unwind fires regardless of drawdown."
            ),
            aschenbrenner_override=True,
            warnings=warnings,
        )

    # 2. Tier 3 by drawdown.
    if drawdown >= TIER_3_DD_THRESHOLD:
        if alloc <= TIER_3_TARGET_ALLOCATION:
            return _hold(
                drawdown,
                alloc,
                f"drawdown={drawdown:.1%} >= 50% but already fully unwound. "
                "No further action.",
                warnings=warnings,
            )
        return _deleverage(
            tier=3,
            drawdown=drawdown,
            alloc=alloc,
            target_alloc=TIER_3_TARGET_ALLOCATION,
            rationale=f"drawdown={drawdown:.1%} >= 50% — full unwind to 0%.",
            warnings=warnings,
        )

    # 3. Tier 2.
    if drawdown >= TIER_2_DD_THRESHOLD:
        if alloc > TIER_2_TARGET_ALLOCATION:
            return _deleverage(
                tier=2,
                drawdown=drawdown,
                alloc=alloc,
                target_alloc=TIER_2_TARGET_ALLOCATION,
                rationale=(
                    f"drawdown={drawdown:.1%} >= 35% AND "
                    f"allocation={alloc:.1%} > 12.5% — deleverage to 12.5%."
                ),
                warnings=warnings,
            )
        return _hold(
            drawdown,
            alloc,
            f"drawdown={drawdown:.1%} >= 35% but allocation={alloc:.1%} "
            "already <= 12.5%. Tier 2 satisfied.",
            warnings=warnings,
        )

    # 4. Tier 1.
    if drawdown >= TIER_1_DD_THRESHOLD:
        if alloc > TIER_1_TARGET_ALLOCATION:
            return _deleverage(
                tier=1,
                drawdown=drawdown,
                alloc=alloc,
                target_alloc=TIER_1_TARGET_ALLOCATION,
                rationale=(
                    f"drawdown={drawdown:.1%} >= 20% AND "
                    f"allocation={alloc:.1%} > 17.5% — deleverage to 17.5%."
                ),
                warnings=warnings,
            )
        return _hold(
            drawdown,
            alloc,
            f"drawdown={drawdown:.1%} >= 20% but allocation={alloc:.1%} "
            "already <= 17.5%. Tier 1 satisfied.",
            warnings=warnings,
        )

    # 5. No trigger — normal monitoring cycle.
    return _hold(
        drawdown,
        alloc,
        f"drawdown={drawdown:.1%} below 20% threshold. No action.",
        warnings=warnings,
    )


def compute_as_trace(inputs: KillSwitchInputs) -> TraceEntry:
    """Compute and wrap in a :class:`TraceEntry` for the event log."""
    decision = compute(inputs)
    return TraceEntry(
        tool=TOOL,
        inputs={
            "thematic_market_value": inputs.thematic_market_value,
            "peak_thematic_value": inputs.peak_thematic_value,
            "total_account_value": inputs.total_account_value,
            "aschenbrenner_kill_event": inputs.aschenbrenner_kill_event,
            "previous_fired_tier": inputs.previous_fired_tier,
        },
        output=decision.to_dict(),
    )
