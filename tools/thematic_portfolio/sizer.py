"""Unified mirror sizer for the thematic-portfolio subagent (Loop 1 Pass 3).

Per [[swing-thematic-portfolio-session-2-design-changes]] design change #1:

    subagent_weight = 1.0 × sa_lp_weight_in_long_book × thematic_allocation
    target_pct_of_total = min(raw, hard_cap_pct)         # hard_cap_pct = 5.0 per Q7

The original design had tier-based bucket caps (6.25% for ≥2-fund consensus
positions; 4% for SA-LP-only positions). Session 2 eliminated those — they
penalised non-consensus positions, which DEFEATS the DeepSeek-style-reproduction
premise (Aschenbrenner's edge IS the non-consensus thesis; the SA-LP-only
small-cap shovels basket is $2.38B / 19 names, 1.6× the boost-tier book).

The current design: 1.0× full mirror, single 5% hard cap, naturally inherits
SA LP's actual concentration structure.

Worked example on Q1 2026 SA LP long book at 25% thematic allocation:

* BE @ 22.8% SA LP weight → 5.7% raw → capped at 5.0% (cap binding)
* SNDK @ 18.8% → 4.7%
* CRWV @ 14.4% → 3.6%
* IREN @ 10.4% → 2.6%
* CORZ @ 10.1% → 2.5%
* APLD @ 8.3% → 2.07%
* RIOT @ 3.7% → 0.92%
* Tail of 14 names @ 0.02-2.7% each → < 0.7% each
* Top-6 cluster ≈ 21% of total portfolio (= 84% of thematic bucket).

Pure arithmetic; no data fetch. The caller supplies a pre-filtered long-book
:class:`Position` list (typically loaded from edgartools output).

CLI::

    uv run python -m tools.thematic_portfolio.sizer --13f-path <path.json> --allocation 25
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..cli import emit
from ..contract import TraceEntry
from . import Position

TOOL = "tools/thematic_portfolio/sizer.py"

DEFAULT_HARD_CAP_PCT = 5.0
DEFAULT_MIRROR_MULTIPLIER = 1.0
VALID_ALLOCATIONS = (10.0, 15.0, 25.0)


def compute(
    sa_lp_long_book: list[Position],
    thematic_allocation_pct: float,
    hard_cap_pct: float = DEFAULT_HARD_CAP_PCT,
    mirror_multiplier: float = DEFAULT_MIRROR_MULTIPLIER,
) -> TraceEntry:
    """Compute mirror weights for every position in the SA LP long book.

    Args:
        sa_lp_long_book: pre-filtered long-only positions (caller drops puts +
            calls before passing in). Must be non-empty.
        thematic_allocation_pct: current Loop 5 phase allocation as percent
            of total portfolio (10.0, 15.0, or 25.0). Other values rejected —
            phasing is a discrete schedule.
        hard_cap_pct: max single-position weight as percent of TOTAL portfolio.
            Default 5.0 per Q7 hard rule. Lower values allowed for stress
            testing (e.g. 3.0 to model the previous tier cap); validator below
            enforces ≤ 5.0 because the framework's CLAUDE.md hard rule is 5%.
        mirror_multiplier: SA LP weight multiplier. Default 1.0 (full mirror)
            per session-2 design change #1. Exposed as a parameter so future
            sensitivity analysis can vary it without forking the tool, but
            production calls should always pass 1.0.

    Returns:
        TraceEntry whose output contains:

        * ``positions``: list of per-position dicts with ``ticker``,
          ``sa_lp_value_usd``, ``sa_lp_weight_pct_of_long_book``,
          ``raw_target_pct_of_total_pre_cap``, ``target_weight_pct_of_total``,
          and ``cap_binding`` ("none" or "total_portfolio_5pct")
        * ``summary``: total long-book value, position count, sum of capped
          weights, number of positions hitting the cap, % of thematic bucket
          consumed by the top-6 cluster

    Raises:
        ValueError: empty book, invalid allocation, negative cap, cap > 5.0,
            multiplier ≤ 0, or any position with non-positive ``value_usd``.
    """
    if not sa_lp_long_book:
        raise ValueError("sa_lp_long_book must be non-empty")
    if thematic_allocation_pct not in VALID_ALLOCATIONS:
        raise ValueError(
            f"thematic_allocation_pct must be one of {VALID_ALLOCATIONS}; "
            f"got {thematic_allocation_pct}"
        )
    if hard_cap_pct <= 0 or hard_cap_pct > 5.0:
        raise ValueError(
            f"hard_cap_pct must be in (0, 5.0]; got {hard_cap_pct} "
            "(CLAUDE.md hard rule)"
        )
    if mirror_multiplier <= 0:
        raise ValueError(f"mirror_multiplier must be positive; got {mirror_multiplier}")
    if any(p.value_usd <= 0 for p in sa_lp_long_book):
        bad = [p.ticker for p in sa_lp_long_book if p.value_usd <= 0]
        raise ValueError(f"positions with non-positive value_usd: {bad}")

    total_value = sum(p.value_usd for p in sa_lp_long_book)
    positions_out: list[dict] = []
    cap_hits = 0

    for p in sa_lp_long_book:
        sa_lp_weight_pct = (p.value_usd / total_value) * 100.0
        raw_target_pct = mirror_multiplier * (sa_lp_weight_pct / 100.0) * thematic_allocation_pct
        capped_target_pct = min(raw_target_pct, hard_cap_pct)
        cap_binding = "total_portfolio_5pct" if raw_target_pct > hard_cap_pct else "none"
        if cap_binding != "none":
            cap_hits += 1
        positions_out.append(
            {
                "ticker": p.ticker,
                "issuer_name": p.issuer_name,
                "sa_lp_value_usd": p.value_usd,
                "sa_lp_weight_pct_of_long_book": sa_lp_weight_pct,
                "raw_target_pct_of_total_pre_cap": raw_target_pct,
                "target_weight_pct_of_total": capped_target_pct,
                "cap_binding": cap_binding,
            }
        )

    # Sort descending by target weight for human-readable output.
    positions_out.sort(key=lambda d: d["target_weight_pct_of_total"], reverse=True)

    sum_capped_weights = sum(p["target_weight_pct_of_total"] for p in positions_out)
    top_6 = positions_out[:6]
    top_6_sum = sum(p["target_weight_pct_of_total"] for p in top_6)
    top_6_share_of_bucket = (
        (top_6_sum / thematic_allocation_pct) if thematic_allocation_pct > 0 else 0.0
    )

    return TraceEntry(
        tool=TOOL,
        inputs={
            "sa_lp_long_book_n_positions": len(sa_lp_long_book),
            "thematic_allocation_pct": thematic_allocation_pct,
            "hard_cap_pct": hard_cap_pct,
            "mirror_multiplier": mirror_multiplier,
        },
        output={
            "positions": positions_out,
            "summary": {
                "total_long_book_value_usd": total_value,
                "n_positions": len(positions_out),
                "sum_capped_target_pct": sum_capped_weights,
                "thematic_allocation_pct": thematic_allocation_pct,
                "thematic_bucket_consumed_pct": (
                    sum_capped_weights / thematic_allocation_pct * 100.0
                ),
                "n_cap_hits": cap_hits,
                "top_6_target_pct": top_6_sum,
                "top_6_share_of_bucket_pct": top_6_share_of_bucket * 100.0,
                "hard_cap_pct": hard_cap_pct,
            },
        },
    )


def load_long_book_from_json(path: Path) -> list[Position]:
    """Load a long-book JSON file as a list of :class:`Position`.

    Expected JSON shape: list of objects with ``ticker``, ``issuer_name``,
    ``value_usd``, and optional ``cusip``. The corpus-ingest module
    (Week 3-4 deliverable) will write 13F long books in this shape after
    filtering out put + call legs.

    Raises:
        ValueError: file is missing required fields.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array; got {type(data).__name__}")
    out: list[Position] = []
    for i, row in enumerate(data):
        missing = {"ticker", "issuer_name", "value_usd"} - set(row.keys())
        if missing:
            raise ValueError(f"{path} row {i} missing fields {missing}")
        out.append(
            Position(
                ticker=row["ticker"],
                issuer_name=row["issuer_name"],
                value_usd=float(row["value_usd"]),
                cusip=row.get("cusip"),
            )
        )
    return out


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.sizer",
        description=(
            "Unified mirror sizer: 1.0 × SA LP long-book weight × thematic allocation, "
            "capped at 5%% of total portfolio (per session-2 design change #1)."
        ),
    )
    p.add_argument(
        "--13f-path",
        dest="long_book_path",
        type=Path,
        required=True,
        help="Path to JSON file containing the pre-filtered SA LP long book.",
    )
    p.add_argument(
        "--allocation",
        dest="thematic_allocation_pct",
        type=float,
        required=True,
        help="Current Loop 5 phase allocation: 10, 15, or 25.",
    )
    p.add_argument(
        "--hard-cap",
        dest="hard_cap_pct",
        type=float,
        default=DEFAULT_HARD_CAP_PCT,
        help=f"Hard cap as %% of total portfolio (default {DEFAULT_HARD_CAP_PCT}).",
    )
    p.add_argument(
        "--multiplier",
        dest="mirror_multiplier",
        type=float,
        default=DEFAULT_MIRROR_MULTIPLIER,
        help=f"Mirror multiplier (default {DEFAULT_MIRROR_MULTIPLIER}; production = 1.0).",
    )
    args = p.parse_args()
    book = load_long_book_from_json(args.long_book_path)
    emit(
        compute(
            sa_lp_long_book=book,
            thematic_allocation_pct=args.thematic_allocation_pct,
            hard_cap_pct=args.hard_cap_pct,
            mirror_multiplier=args.mirror_multiplier,
        )
    )


if __name__ == "__main__":
    main()
