"""ADD-ON evaluator — should this leg fire? At what size? New stop?

Per ``swing-momentum-execution.md`` decision logic:

* **ADD-ON #1** (Stage-2): triggered by Momentum Burst; brings position
  from 1/3 intended → full intended; stop migrates to combined break-even
  on the now-merged STARTER + ADD-ON.
* **ADD-ON #2** (Stage-3): triggered by Day 7 milestone survival; adds
  50% on top of full size → 1.5× intended; stop tightens to 10-day MA.
  **Only fires for Super Swan / Golden EP setup grades.**
* Hard gates: broad-market regime not Stage 3+, setup grade qualifying,
  concentration cap not breached.

Pure composition over :mod:`tools.combined_breakeven` and the trigger
detectors. Caller supplies the position state + trigger booleans + grade
+ regime — this tool decides go/no-go and the post-add stop.

CLI::

    uv run python -m tools.add_on_evaluator --stage STARTER \\
        --starter-shares 20 --starter-price 415.80 --intended-shares 60 \\
        --triggered --setup-grade GoldenEP --regime stage_2_confirmed \\
        --current-price 448.20
"""
from __future__ import annotations

import argparse

from .cli import emit
from .combined_breakeven import compute as combined_compute
from .contract import TraceEntry

TOOL = "tools/add_on_evaluator.py"

# Per swing-momentum-execution.md Day-7 add restricted to high-grade setups.
DAY7_QUALIFIED_GRADES = {"SuperSwan", "GoldenEP"}
STAGE_2_ALLOWED_REGIMES = {"stage_2_confirmed", "stage_2_weakening"}
STAGE_3_ALLOWED_REGIMES = {"stage_2_confirmed"}   # tighter per playbook


def compute(
    current_stage: str,
    triggered: bool,
    setup_grade: str,
    regime_class: str,
    starter_shares: int,
    starter_price: float,
    intended_full_shares: int,
    current_price: float,
    addon1_shares: int | None = None,
    addon1_price: float | None = None,
    addon2_max_pct: float = 0.50,
    chase_distance_pct: float = 0.05,
    concentration_cap_shares: int | None = None,
) -> TraceEntry:
    """Evaluate ADD-ON eligibility + return add quantity + new stop.

    Args:
        current_stage: ``STARTER`` (evaluating add #1) or ``Stage-2``
            (evaluating add #2). Anything else = no-op.
        triggered: True iff the relevant detector fired (Momentum Burst
            for add #1, Day 7 milestone for add #2).
        setup_grade: per swing-position-sizing grade keys.
        regime_class: per swing-regime-playbook regime keys.
        starter_shares / starter_price: original STARTER leg.
        intended_full_shares: full intended position (from ledger).
        current_price: current market price; used for chase-distance check.
        addon1_shares / addon1_price: when evaluating ADD-ON #2.
        addon2_max_pct: fraction of full size to add at Stage-3.
            Default 0.50 (= 1.5× intended).
        chase_distance_pct: if price is extended >5% above last leg, the
            add is a chase — skip.
        concentration_cap_shares: optional hard ceiling. If the proposed
            total exceeds this, the add is capped to fit.

    Returns:
        TraceEntry with action ∈ {add, skip, no_op}, reason, add_shares,
        new_total_shares, new_combined_breakeven, new_stop, stage_after.
    """
    if current_stage not in {"STARTER", "Stage-2"}:
        return TraceEntry(
            tool=TOOL,
            inputs={"current_stage": current_stage},
            output={"action": "no_op", "reason": f"no add eligible from stage {current_stage}"},
        )

    if not triggered:
        return TraceEntry(
            tool=TOOL,
            inputs={"current_stage": current_stage, "triggered": False},
            output={"action": "skip", "reason": "trigger not fired"},
        )

    # Regime gates.
    if current_stage == "STARTER":
        allowed_regimes = STAGE_2_ALLOWED_REGIMES
        evaluating = "ADD-ON #1"
    else:
        allowed_regimes = STAGE_3_ALLOWED_REGIMES
        evaluating = "ADD-ON #2"
    if regime_class not in allowed_regimes:
        return TraceEntry(
            tool=TOOL,
            inputs={
                "current_stage": current_stage,
                "regime_class": regime_class,
                "evaluating": evaluating,
            },
            output={
                "action": "skip",
                "reason": (
                    f"{evaluating} blocked by regime {regime_class!r}; "
                    f"requires one of {sorted(allowed_regimes)}"
                ),
            },
        )

    # Grade gate for ADD-ON #2 only.
    if current_stage == "Stage-2" and setup_grade not in DAY7_QUALIFIED_GRADES:
        return TraceEntry(
            tool=TOOL,
            inputs={"setup_grade": setup_grade, "evaluating": evaluating},
            output={
                "action": "skip",
                "reason": (
                    f"ADD-ON #2 restricted to {sorted(DAY7_QUALIFIED_GRADES)}; "
                    f"setup_grade={setup_grade!r}"
                ),
            },
        )

    # Chase check: refuse add if price is extended above last filled leg.
    last_leg_price = (
        addon1_price if current_stage == "Stage-2" and addon1_price is not None else starter_price
    )
    if current_price > last_leg_price * (1.0 + chase_distance_pct):
        return TraceEntry(
            tool=TOOL,
            inputs={
                "current_price": current_price,
                "last_leg_price": last_leg_price,
                "chase_distance_pct": chase_distance_pct,
            },
            output={
                "action": "skip",
                "reason": (
                    f"chase detected: current={current_price} > last_leg*"
                    f"{1 + chase_distance_pct:.2f} ({last_leg_price * (1 + chase_distance_pct):.2f})"
                ),
            },
        )

    # Compute add size.
    if current_stage == "STARTER":
        # Bring from 1/3 (= starter) up to full intended.
        add_shares = max(0, intended_full_shares - starter_shares)
        new_total = intended_full_shares
    else:
        # Add up to addon2_max_pct of full intended (typically 50%).
        add_shares = int(intended_full_shares * addon2_max_pct)
        existing = starter_shares + (addon1_shares or 0)
        new_total = existing + add_shares

    if add_shares <= 0:
        return TraceEntry(
            tool=TOOL,
            inputs={"add_shares_requested": add_shares},
            output={"action": "skip", "reason": "computed add_shares <= 0"},
        )

    # Concentration cap.
    if concentration_cap_shares is not None and new_total > concentration_cap_shares:
        capped = max(0, concentration_cap_shares - (new_total - add_shares))
        if capped <= 0:
            return TraceEntry(
                tool=TOOL,
                inputs={
                    "concentration_cap_shares": concentration_cap_shares,
                    "current_total_pre_add": new_total - add_shares,
                },
                output={
                    "action": "skip",
                    "reason": "concentration_cap_shares already met or exceeded",
                },
            )
        add_shares = capped
        new_total = (new_total - add_shares) + capped

    # Compute new combined break-even + new stop.
    legs: list[tuple[int, float]] = [(starter_shares, starter_price)]
    if current_stage == "Stage-2":
        legs.append((addon1_shares, addon1_price))
    legs.append((add_shares, current_price))
    breakeven_entry = combined_compute(legs)
    new_breakeven = breakeven_entry.output["combined_breakeven"]

    if current_stage == "STARTER":
        # ADD-ON #1: stop migrates to combined break-even.
        new_stop = new_breakeven
        trail_ma = "combined_breakeven"
        stage_after = "Stage-2"
    else:
        # ADD-ON #2: trail tightens to 10-day MA. Stop set to current_price
        # is a conservative placeholder — the actual stop is the 10-day MA
        # level which the caller should re-compute via trend_template. Here
        # we leave new_stop as the existing combined break-even and signal
        # the trail change.
        new_stop = max(new_breakeven, last_leg_price)  # conservative
        trail_ma = "10_day_MA"
        stage_after = "Stage-3"

    return TraceEntry(
        tool=TOOL,
        inputs={
            "current_stage": current_stage,
            "triggered": triggered,
            "setup_grade": setup_grade,
            "regime_class": regime_class,
            "starter_shares": starter_shares,
            "starter_price": starter_price,
            "intended_full_shares": intended_full_shares,
            "current_price": current_price,
            "addon1_shares": addon1_shares,
            "addon1_price": addon1_price,
            "addon2_max_pct": addon2_max_pct,
            "chase_distance_pct": chase_distance_pct,
            "concentration_cap_shares": concentration_cap_shares,
        },
        output={
            "action": "add",
            "evaluating": evaluating,
            "add_shares": add_shares,
            "new_total_shares": new_total,
            "new_combined_breakeven": new_breakeven,
            "new_stop": new_stop,
            "trail_ma": trail_ma,
            "stage_after": stage_after,
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.add_on_evaluator",
        description="ADD-ON eligibility + size + new stop.",
    )
    p.add_argument("--stage", required=True, choices=["STARTER", "Stage-2", "Stage-3", "trailing", "closed"], dest="current_stage")
    p.add_argument("--triggered", action="store_true")
    p.add_argument("--setup-grade", required=True)
    p.add_argument("--regime", required=True, dest="regime_class")
    p.add_argument("--starter-shares", type=int, required=True)
    p.add_argument("--starter-price", type=float, required=True)
    p.add_argument("--intended-shares", type=int, required=True, dest="intended_full_shares")
    p.add_argument("--current-price", type=float, required=True)
    p.add_argument("--addon1-shares", type=int, default=None)
    p.add_argument("--addon1-price", type=float, default=None)
    p.add_argument("--concentration-cap-shares", type=int, default=None)
    args = p.parse_args()
    emit(
        compute(
            current_stage=args.current_stage,
            triggered=args.triggered,
            setup_grade=args.setup_grade,
            regime_class=args.regime_class,
            starter_shares=args.starter_shares,
            starter_price=args.starter_price,
            intended_full_shares=args.intended_full_shares,
            current_price=args.current_price,
            addon1_shares=args.addon1_shares,
            addon1_price=args.addon1_price,
            concentration_cap_shares=args.concentration_cap_shares,
        )
    )


if __name__ == "__main__":
    main()
