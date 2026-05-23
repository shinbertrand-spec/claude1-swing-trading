"""Combined-position weighted-average entry (per ``swing-momentum-execution.md``).

The Anchor-and-Pyramid workflow migrates the stop to **break-even on the
COMBINED position** after each ADD-ON. That break-even is the weighted
average of the legs filled so far. Pure arithmetic — no data fetch.

Examples
--------

STARTER 20 sh @ $415.80 → ADD-ON #1 40 sh @ $448.20:
    combined = (20*415.80 + 40*448.20) / 60 = $437.40

After ADD-ON #2 30 sh @ $465.40:
    combined = (20*415.80 + 40*448.20 + 30*465.40) / 90 = $446.74

CLI::

    uv run python -m tools.combined_breakeven \\
        --leg 20:415.80 --leg 40:448.20 --leg 30:465.40
"""
from __future__ import annotations

import argparse

from .cli import emit
from .contract import TraceEntry

TOOL = "tools/combined_breakeven.py"


def compute(legs: list[tuple[int, float]]) -> TraceEntry:
    """Weighted-average entry across legs.

    Args:
        legs: list of (shares, fill_price) tuples in fill order.

    Raises:
        ValueError: if any leg has non-positive shares or price.
    """
    if not legs:
        raise ValueError("legs must be non-empty")
    total_shares = 0
    total_cost = 0.0
    for i, (shares, price) in enumerate(legs):
        if shares <= 0:
            raise ValueError(f"leg {i}: shares must be positive; got {shares}")
        if price <= 0:
            raise ValueError(f"leg {i}: price must be positive; got {price}")
        total_shares += shares
        total_cost += shares * price
    avg = total_cost / total_shares
    return TraceEntry(
        tool=TOOL,
        inputs={"legs": [list(leg) for leg in legs]},
        output={
            "combined_breakeven": avg,
            "total_shares": total_shares,
            "total_cost": total_cost,
            "leg_count": len(legs),
        },
    )


def _parse_leg(s: str) -> tuple[int, float]:
    try:
        shares_s, price_s = s.split(":", 1)
        return int(shares_s), float(price_s)
    except (ValueError, AttributeError) as exc:
        raise argparse.ArgumentTypeError(
            f"leg must be 'shares:price' (e.g. '20:415.80'); got {s!r}"
        ) from exc


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.combined_breakeven",
        description="Weighted-average entry across pyramided legs.",
    )
    p.add_argument(
        "--leg",
        type=_parse_leg,
        action="append",
        required=True,
        help="Repeatable. Format 'shares:price' e.g. '20:415.80'",
    )
    args = p.parse_args()
    emit(compute(args.leg))


if __name__ == "__main__":
    main()
