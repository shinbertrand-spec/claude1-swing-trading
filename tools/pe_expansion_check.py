"""P/E expansion late-stage warning (per ``swing-sell-discipline.md``).

# v1-preliminary: revisit after Minervini book v2 ingestion

"P/E expansion doubled or more during late-stage price action" is an
additional sell warning per the operational note — late-cycle buyers are
paying up regardless of valuation.

Pure compute. Caller provides a P/E time series (or recent/baseline
values). Phase 2: no live fundamental-history fetch yet — caller supplies
the values.

CLI::

    uv run python -m tools.pe_expansion_check --baseline 18.0 --current 38.0
"""
from __future__ import annotations

import argparse

from .cli import emit
from .contract import TraceEntry

TOOL = "tools/pe_expansion_check.py"

DEFAULT_EXPANSION_THRESHOLD = 2.0   # doubled


def compute(
    baseline_pe: float,
    current_pe: float,
    threshold_ratio: float = DEFAULT_EXPANSION_THRESHOLD,
) -> TraceEntry:
    """Did the P/E ratio double (or by ``threshold_ratio``) since baseline?

    Args:
        baseline_pe: P/E earlier in the move (e.g. at base breakout).
        current_pe: current P/E.
        threshold_ratio: expansion multiple that fires the warning.
            Default 2.0 (doubled).

    Raises:
        ValueError: if either P/E is non-positive (negative-earnings or
            missing-data conditions caller must handle separately).
    """
    if baseline_pe <= 0:
        raise ValueError(f"baseline_pe must be positive; got {baseline_pe}")
    if current_pe <= 0:
        raise ValueError(f"current_pe must be positive; got {current_pe}")
    expansion_ratio = current_pe / baseline_pe
    expanded = expansion_ratio >= threshold_ratio
    return TraceEntry(
        tool=TOOL,
        inputs={
            "baseline_pe": baseline_pe,
            "current_pe": current_pe,
            "threshold_ratio": threshold_ratio,
            "v1_preliminary": True,
        },
        output={
            "pe_expanded": expanded,
            "expansion_ratio": expansion_ratio,
            "warning_late_stage": expanded,
            "v1_preliminary_flag": True,
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.pe_expansion_check",
        description="P/E expansion late-stage warning. v1-preliminary.",
    )
    p.add_argument("--baseline", type=float, required=True, dest="baseline_pe")
    p.add_argument("--current", type=float, required=True, dest="current_pe")
    p.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_EXPANSION_THRESHOLD,
        dest="threshold_ratio",
    )
    args = p.parse_args()
    emit(
        compute(
            baseline_pe=args.baseline_pe,
            current_pe=args.current_pe,
            threshold_ratio=args.threshold_ratio,
        )
    )


if __name__ == "__main__":
    main()
