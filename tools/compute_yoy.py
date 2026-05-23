"""Compute year-over-year growth as a decimal fraction.

Canonical Requirement 2 example: ``eps_yoy_growth = (eps_curr / eps_prior) - 1``
moves out of LLM prose into a deterministic three-line function. The same tool
serves EPS YoY, revenue YoY, and any other ratio comparison.

CLI::

    uv run python -m tools.compute_yoy 1.87 1.55
"""
from __future__ import annotations

import argparse

from .cli import emit
from .contract import TraceEntry

TOOL = "tools/compute_yoy.py"


def compute(current: float, prior: float) -> TraceEntry:
    """Return YoY growth: ``(current / prior) - 1``.

    Args:
        current: most recent period value.
        prior: same-period-one-year-prior value.

    Raises:
        ValueError: if ``prior`` is zero or negative (undefined / sign-flip
            ambiguity makes the ratio meaningless).
    """
    if prior <= 0:
        raise ValueError(f"prior must be positive; got {prior}")
    yoy = (current / prior) - 1.0
    return TraceEntry(
        tool=TOOL,
        inputs={"current": current, "prior": prior},
        output={"yoy_growth_decimal": yoy, "yoy_growth_pct": yoy * 100.0},
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.compute_yoy",
        description="Compute YoY growth as decimal fraction.",
    )
    p.add_argument("current", type=float, help="Most recent period value")
    p.add_argument("prior", type=float, help="Same-period-one-year-prior value")
    args = p.parse_args()
    emit(compute(args.current, args.prior))


if __name__ == "__main__":
    main()
