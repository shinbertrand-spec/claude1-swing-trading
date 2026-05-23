"""Sell-decision composer (per ``swing-sell-discipline.md``).

# v1-preliminary: revisit after Minervini book v2 ingestion

Composes the climax-top count, violations count, base-stage check, P/E
expansion warning, and sell-into-strength signal into a single recommended
action. Setup-grade modifies the thresholds per the operational note's
modifier table.

Decision matrices (from swing-sell-discipline):

Climax-top patterns firing:
    0-1 → hold (normal trail)
    2   → sell 50%, tighten to 5-day MA
    3+  → sell 75-100% immediately

Violations firing:
    0 → hold
    1 → tighten + partial 1/3
    2 → sell 50%+, tighten to 5-day MA
    3+ → full exit
    Special: violation #5 alone (close below 20/50 MA on heavy vol) = full exit

Late-stage base + new high:
    1st-2nd stage → hold
    3rd stage → sell 1/3, tighter trail
    4th-5th stage on new high → sell 50-100%

In-doubt rule (Minervini): when triggers conflict, default to sell 50%
(eliminates emotional asymmetry).

When multiple triggers fire, **take the most aggressive action**.

Pure compute — caller supplies the detector outputs + setup grade +
regime + sell-into-strength signal.

CLI: not provided (orchestrator-level composition; use library import).
"""
from __future__ import annotations

from .contract import TraceEntry

TOOL = "tools/sell_decision.py"

# Per swing-sell-discipline.md setup-grade modifier table.
GRADE_MODIFIER = {
    "A+":       {"climax_threshold": 3, "violation_threshold": 3},
    "SuperSwan":{"climax_threshold": 3, "violation_threshold": 3},
    "GoldenEP": {"climax_threshold": 3, "violation_threshold": 3},
    "A":        {"climax_threshold": 2, "violation_threshold": 2},
    "Swan":     {"climax_threshold": 2, "violation_threshold": 2},
    "B":        {"climax_threshold": 2, "violation_threshold": 2},
    "Duck":     {"climax_threshold": 2, "violation_threshold": 2},
    "C":        {"climax_threshold": 1, "violation_threshold": 1},
    "Chicken":  {"climax_threshold": 1, "violation_threshold": 1},
}

# Severity rank for action aggregation; higher = more aggressive.
ACTION_RANK = {
    "hold": 0,
    "tighten_stop": 1,
    "sell_1_3": 2,
    "sell_50": 3,
    "sell_75": 4,
    "sell_100": 5,
}


def _max_action(actions: list[str]) -> str:
    return max(actions, key=lambda a: ACTION_RANK.get(a, -1))


def compute(
    climax_patterns_firing: int,
    violations_firing: int,
    violation_5_alone_full_exit: bool,
    base_stage: int,
    new_high_today: bool,
    sell_into_strength_triggered: bool,
    sell_into_strength_fraction: float,
    setup_grade: str,
    pe_expansion_warning: bool = False,
    regime_class: str = "stage_2_confirmed",
) -> TraceEntry:
    """Compose into a single sell recommendation.

    Args:
        climax_patterns_firing: 0-6 from ``climax_top_detect``.
        violations_firing: 0-5 from ``violations_detect``.
        violation_5_alone_full_exit: from ``violations_detect`` — close
            below 20/50 MA on heavy volume is a standalone full-exit
            trigger regardless of other violation count.
        base_stage: 1-5 from ``base_stage_detect``.
        new_high_today: from ``base_stage_detect``.
        sell_into_strength_triggered: from ``sell_into_strength``.
        sell_into_strength_fraction: 0.0 / 0.50 / 0.80 from
            ``sell_into_strength``.
        setup_grade: per swing-position-sizing grade keys.
        pe_expansion_warning: from ``pe_expansion_check``.
        regime_class: per swing-regime-playbook. Stage 4 forces exit-all.

    Returns:
        TraceEntry with ``action``, ``confidence`` (HIGH/MEDIUM/LOW),
        ``contributing_triggers``, ``in_doubt_default_applied``.
    """
    if setup_grade not in GRADE_MODIFIER:
        raise ValueError(
            f"unknown setup_grade {setup_grade!r}; known: {sorted(GRADE_MODIFIER)}"
        )

    # Stage 4 broad market = exit all per swing-regime-playbook.
    if regime_class == "stage_4":
        return TraceEntry(
            tool=TOOL,
            inputs={"regime_class": regime_class, "v1_preliminary": True},
            output={
                "action": "sell_100",
                "confidence": "HIGH",
                "contributing_triggers": ["regime_stage_4_exit_all"],
                "in_doubt_default_applied": False,
                "v1_preliminary_flag": True,
            },
        )

    mod = GRADE_MODIFIER[setup_grade]
    contributing: list[str] = []
    proposed_actions: list[str] = []

    # Climax-top decision per the operational matrix (modifier-aware).
    if climax_patterns_firing >= max(3, mod["climax_threshold"]):
        proposed_actions.append("sell_75")
        contributing.append(f"climax_top_3plus (count={climax_patterns_firing})")
    elif climax_patterns_firing >= 2 and 2 >= mod["climax_threshold"]:
        proposed_actions.append("sell_50")
        proposed_actions.append("tighten_stop")
        contributing.append(f"climax_top_2 (count={climax_patterns_firing})")
    elif climax_patterns_firing >= 1 and mod["climax_threshold"] == 1:
        proposed_actions.append("sell_50")
        contributing.append(f"climax_top_low_grade (count={climax_patterns_firing})")

    # Violations decision.
    if violation_5_alone_full_exit:
        proposed_actions.append("sell_100")
        contributing.append("violation_5_close_below_MA_heavy_volume_full_exit")
    elif violations_firing >= 3:
        proposed_actions.append("sell_100")
        contributing.append(f"violations_3plus (count={violations_firing})")
    elif violations_firing >= 2 and 2 >= mod["violation_threshold"]:
        proposed_actions.append("sell_50")
        proposed_actions.append("tighten_stop")
        contributing.append(f"violations_2 (count={violations_firing})")
    elif violations_firing >= 1 and mod["violation_threshold"] == 1:
        proposed_actions.append("sell_50")
        contributing.append(f"violations_low_grade (count={violations_firing})")
    elif violations_firing >= 1:
        proposed_actions.append("tighten_stop")
        proposed_actions.append("sell_1_3")
        contributing.append(f"violations_1 (count={violations_firing})")

    # Late-stage base + new high.
    if base_stage >= 4 and new_high_today:
        proposed_actions.append("sell_75")
        contributing.append(f"late_stage_new_high (base_stage={base_stage})")
    elif base_stage == 3 and new_high_today:
        proposed_actions.append("sell_1_3")
        proposed_actions.append("tighten_stop")
        contributing.append("3rd_stage_new_high")

    # P/E expansion warning — additive, not standalone.
    if pe_expansion_warning:
        proposed_actions.append("tighten_stop")
        contributing.append("pe_expansion_late_stage_warning")

    # Sell-into-strength rule (3-school convergent).
    if sell_into_strength_triggered:
        if sell_into_strength_fraction >= 0.75:
            proposed_actions.append("sell_75")
        elif sell_into_strength_fraction >= 0.50:
            proposed_actions.append("sell_50")
        contributing.append(
            f"sell_into_strength (fraction={sell_into_strength_fraction})"
        )

    # If nothing fired, hold.
    if not proposed_actions:
        return TraceEntry(
            tool=TOOL,
            inputs={
                "climax_patterns_firing": climax_patterns_firing,
                "violations_firing": violations_firing,
                "violation_5_alone_full_exit": violation_5_alone_full_exit,
                "base_stage": base_stage,
                "new_high_today": new_high_today,
                "sell_into_strength_triggered": sell_into_strength_triggered,
                "sell_into_strength_fraction": sell_into_strength_fraction,
                "setup_grade": setup_grade,
                "pe_expansion_warning": pe_expansion_warning,
                "regime_class": regime_class,
                "v1_preliminary": True,
            },
            output={
                "action": "hold",
                "confidence": "HIGH",
                "contributing_triggers": [],
                "in_doubt_default_applied": False,
                "v1_preliminary_flag": True,
            },
        )

    # In-doubt default: if multiple distinct trigger families propose
    # conflicting actions (some hold-like, some exit-like), default to
    # sell_50 per Minervini's 50%-in-doubt rule.
    distinct_actions = set(proposed_actions)
    in_doubt = (
        len(distinct_actions) >= 3
        and any(a in {"sell_75", "sell_100"} for a in distinct_actions)
        and "tighten_stop" in distinct_actions
    )
    if in_doubt:
        action = "sell_50"
        confidence = "MEDIUM"
    else:
        action = _max_action(proposed_actions)
        confidence = "HIGH" if len(contributing) >= 2 else "MEDIUM"

    return TraceEntry(
        tool=TOOL,
        inputs={
            "climax_patterns_firing": climax_patterns_firing,
            "violations_firing": violations_firing,
            "violation_5_alone_full_exit": violation_5_alone_full_exit,
            "base_stage": base_stage,
            "new_high_today": new_high_today,
            "sell_into_strength_triggered": sell_into_strength_triggered,
            "sell_into_strength_fraction": sell_into_strength_fraction,
            "setup_grade": setup_grade,
            "pe_expansion_warning": pe_expansion_warning,
            "regime_class": regime_class,
            "v1_preliminary": True,
        },
        output={
            "action": action,
            "confidence": confidence,
            "contributing_triggers": contributing,
            "in_doubt_default_applied": in_doubt,
            "proposed_actions": proposed_actions,
            "grade_modifier": mod,
            "v1_preliminary_flag": True,
        },
    )
