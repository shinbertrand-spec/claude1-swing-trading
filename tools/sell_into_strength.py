"""Sell-into-strength check (per ``swing-sell-discipline.md`` — 3-school convergent).

# v1-preliminary: revisit after Minervini book v2 ingestion

Always-on rule across Minervini + Bonde + Kullamägi:

    If position is up 10-15% in 2-3 days and no further catalyst pending,
    sell 50-80% of position.

The conservative interpretation (50%) when conviction is medium; the
aggressive interpretation (80%) when momentum signals exhaustion. Setup
grade modifies — A+ / GoldenEP / SuperSwan default to 50%, B/C/Duck/Chicken
default to 80%.

Pure compute.

CLI::

    uv run python -m tools.sell_into_strength --gain-pct 0.12 --days 2 --grade GoldenEP
"""
from __future__ import annotations

import argparse

from .cli import emit
from .contract import TraceEntry

TOOL = "tools/sell_into_strength.py"

GAIN_MIN = 0.10          # 10%
GAIN_MAX_DAYS = 3
HIGH_GRADE_FRACTION = 0.50
LOW_GRADE_FRACTION = 0.80

HIGH_CONVICTION_GRADES = {"A+", "GoldenEP", "SuperSwan"}


def compute(
    gain_pct: float,
    days_in_move: int,
    setup_grade: str,
    catalyst_pending: bool = False,
) -> TraceEntry:
    """Should we sell into strength right now?

    Args:
        gain_pct: position gain as decimal over the recent move
            (e.g. 0.12 = 12%).
        days_in_move: number of trading days the gain accumulated over.
        setup_grade: per swing-position-sizing grade keys.
        catalyst_pending: True iff a fresh catalyst is expected in the
            window (e.g. upcoming partnership announcement). If True,
            the rule is suppressed.

    Returns:
        TraceEntry with ``threshold_met``, ``recommended_fraction``
        (0.50 / 0.80 / 0.0 if not met), ``rationale``.
    """
    if days_in_move <= 0:
        raise ValueError(f"days_in_move must be positive; got {days_in_move}")

    rationale: list[str] = []
    threshold_met = (
        gain_pct >= GAIN_MIN
        and days_in_move <= GAIN_MAX_DAYS
        and not catalyst_pending
    )

    if not threshold_met:
        if catalyst_pending:
            rationale.append("rule suppressed: catalyst_pending=True")
        elif gain_pct < GAIN_MIN:
            rationale.append(f"gain_pct {gain_pct:.4f} below {GAIN_MIN}")
        elif days_in_move > GAIN_MAX_DAYS:
            rationale.append(f"days_in_move {days_in_move} above {GAIN_MAX_DAYS}")
        fraction = 0.0
    else:
        if setup_grade in HIGH_CONVICTION_GRADES:
            fraction = HIGH_GRADE_FRACTION
            rationale.append(
                f"high-conviction grade {setup_grade} → conservative 50% trim"
            )
        else:
            fraction = LOW_GRADE_FRACTION
            rationale.append(
                f"non-top grade {setup_grade} → aggressive 80% trim"
            )
        rationale.append(
            f"gain={gain_pct:.4f} in {days_in_move}d → sell-into-strength triggered"
        )

    return TraceEntry(
        tool=TOOL,
        inputs={
            "gain_pct": gain_pct,
            "days_in_move": days_in_move,
            "setup_grade": setup_grade,
            "catalyst_pending": catalyst_pending,
            "v1_preliminary": True,
        },
        output={
            "threshold_met": threshold_met,
            "recommended_fraction": fraction,
            "rationale": rationale,
            "v1_preliminary_flag": True,
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.sell_into_strength",
        description="Sell-into-strength (10-15% in 2-3 days). v1-preliminary.",
    )
    p.add_argument("--gain-pct", type=float, required=True)
    p.add_argument("--days", type=int, required=True, dest="days_in_move")
    p.add_argument("--grade", required=True, dest="setup_grade")
    p.add_argument("--catalyst-pending", action="store_true")
    args = p.parse_args()
    emit(
        compute(
            gain_pct=args.gain_pct,
            days_in_move=args.days_in_move,
            setup_grade=args.setup_grade,
            catalyst_pending=args.catalyst_pending,
        )
    )


if __name__ == "__main__":
    main()
