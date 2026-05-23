"""Position state classifier (per ``swing-momentum-execution.md``).

Given a position ledger's ``position_state`` block + current price, returns
the canonical lifecycle stage + a status snapshot. Pure compute; the
caller supplies the position record (typically loaded from
``ledgers/positions/<TICKER>.yml``).

Stages:

* ``STARTER``   — only the starter leg is filled
* ``Stage-2``   — starter + addon_1 filled
* ``Stage-3``   — starter + addon_1 + addon_2 filled (1.5× intended)
* ``trailing``  — full size, no further adds expected
* ``closed``    — position exited
* ``invalid``   — the input position record violates leg-order constraints

CLI::

    uv run python -m tools.position_state \\
        --starter-shares 20 --starter-price 415.80 \\
        --addon1-shares 40 --addon1-price 448.20 \\
        --current-price 478.20
"""
from __future__ import annotations

import argparse

from .cli import emit
from .contract import TraceEntry
from .combined_breakeven import compute as combined_compute

TOOL = "tools/position_state.py"


def compute(
    starter_shares: int | None,
    starter_price: float | None,
    addon1_shares: int | None = None,
    addon1_price: float | None = None,
    addon2_shares: int | None = None,
    addon2_price: float | None = None,
    current_price: float | None = None,
    closed: bool = False,
) -> TraceEntry:
    """Classify the position stage from filled-leg presence + current price.

    Args:
        starter_shares / starter_price: STARTER leg (required for any
            stage other than ``closed``).
        addon1_shares / addon1_price: ADD-ON #1 leg, if filled.
        addon2_shares / addon2_price: ADD-ON #2 leg, if filled.
        current_price: optional — included in output for P&L snapshot.
        closed: True iff the position has been exited.

    Returns:
        TraceEntry with output: ``stage``, ``total_shares``,
        ``combined_breakeven``, ``unrealized_pnl_pct`` (if current_price
        given), and a ``legs`` echo for audit.
    """
    if closed:
        return TraceEntry(
            tool=TOOL,
            inputs={"closed": True},
            output={"stage": "closed", "total_shares": 0},
        )

    if starter_shares is None or starter_price is None:
        raise ValueError("starter leg required (shares + price) for an open position")

    legs: list[tuple[int, float]] = [(starter_shares, starter_price)]
    addon1_present = addon1_shares is not None and addon1_price is not None
    addon2_present = addon2_shares is not None and addon2_price is not None

    if addon2_present and not addon1_present:
        return TraceEntry(
            tool=TOOL,
            inputs={
                "starter": [starter_shares, starter_price],
                "addon_1": None,
                "addon_2": [addon2_shares, addon2_price],
            },
            output={
                "stage": "invalid",
                "error": "addon_2 present without addon_1; pyramiding stages must fill in order",
            },
        )

    if addon1_present:
        legs.append((addon1_shares, addon1_price))
    if addon2_present:
        legs.append((addon2_shares, addon2_price))

    if len(legs) == 1:
        stage = "STARTER"
    elif len(legs) == 2:
        stage = "Stage-2"
    else:
        stage = "Stage-3"

    breakeven_entry = combined_compute(legs)
    total_shares = breakeven_entry.output["total_shares"]
    breakeven = breakeven_entry.output["combined_breakeven"]

    out: dict = {
        "stage": stage,
        "total_shares": total_shares,
        "combined_breakeven": breakeven,
        "legs": {
            "starter": [starter_shares, starter_price],
            "addon_1": [addon1_shares, addon1_price] if addon1_present else None,
            "addon_2": [addon2_shares, addon2_price] if addon2_present else None,
        },
    }
    if current_price is not None:
        out["current_price"] = current_price
        out["unrealized_pnl_pct"] = (current_price / breakeven - 1.0) if breakeven > 0 else 0.0
        out["unrealized_pnl_usd"] = (current_price - breakeven) * total_shares

    return TraceEntry(
        tool=TOOL,
        inputs={
            "starter": [starter_shares, starter_price],
            "addon_1": [addon1_shares, addon1_price] if addon1_present else None,
            "addon_2": [addon2_shares, addon2_price] if addon2_present else None,
            "current_price": current_price,
            "closed": closed,
        },
        output=out,
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.position_state",
        description="Classify pyramid stage from filled legs.",
    )
    p.add_argument("--starter-shares", type=int, default=None)
    p.add_argument("--starter-price", type=float, default=None)
    p.add_argument("--addon1-shares", type=int, default=None)
    p.add_argument("--addon1-price", type=float, default=None)
    p.add_argument("--addon2-shares", type=int, default=None)
    p.add_argument("--addon2-price", type=float, default=None)
    p.add_argument("--current-price", type=float, default=None)
    p.add_argument("--closed", action="store_true")
    args = p.parse_args()
    emit(
        compute(
            starter_shares=args.starter_shares,
            starter_price=args.starter_price,
            addon1_shares=args.addon1_shares,
            addon1_price=args.addon1_price,
            addon2_shares=args.addon2_shares,
            addon2_price=args.addon2_price,
            current_price=args.current_price,
            closed=args.closed,
        )
    )


if __name__ == "__main__":
    main()
