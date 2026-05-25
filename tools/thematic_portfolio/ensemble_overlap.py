"""Ensemble overlap + critic-trigger context (Loop 1 Passes 4 + downstream calibration).

Per [[swing-thematic-portfolio-session-2-design-changes]] design changes #3,
#4, and #5; per [[swing-thematic-portfolio-q4-calibration-metric]] for M1/M3.

Three primary computations:

1. **M1 — position-set Jaccard** (calibration M1, session-2 #3):

       jaccard = |SA_LP ∩ subagent| / |SA_LP ∪ subagent|

   Used by Loop 2 calibration. ≥ 0.85 → pass; otherwise flag for retrain.

2. **M3 — ensemble triangulation, RANK-based** (session-2 #4):

   For top-K positions in SA LP long book, what fraction also appear in
   the union of top-K positions across the ensemble funds (Altimeter +
   Coatue + Light Street)? Rank-based, NOT notional-weighted — Light
   Street's $0.50B is 50× smaller than Coatue's $29.06B; notional weight
   drowns Light Street's signal. ≥ 0.5 → consensus health (informational —
   does NOT trigger retrain).

3. **Critic-trigger context** (Loop 1 Pass 4, per session-2 #5 pseudocode):

   For one position, compute which ensemble funds hold it this quarter,
   which exited since last quarter, and which of four trigger rules applies:
   ``ensemble_disagreement`` / ``sa_lp_doubling_down_vs_consensus_exit`` /
   ``non_consensus_sa_lp_solo`` / ``none``. Emitted per-position by Loop 1
   for the downstream critic dispatch to consume.

**M2 (critic-outcome alignment over rolling 4q)** is NOT in this module —
it requires accumulated 4-quarter critic decision history, which only exists
after Loop 1 has fired across ≥ 4 quarters in paper-trade. Deferred to a
later module (Weeks 5-8 paper-trade phase).

Pure arithmetic over pre-loaded :class:`Position` lists. No data fetch.

CLI (computes all three at once if all inputs provided)::

    uv run python -m tools.thematic_portfolio.ensemble_overlap \\
        --sa-lp <path>.json \\
        --altimeter <path>.json --coatue <path>.json --light-street <path>.json \\
        --sa-lp-prior <path>.json --altimeter-prior <path>.json \\
        --coatue-prior <path>.json --light-street-prior <path>.json \\
        [--subagent <path>.json --jaccard-only]
        [--position-trigger <TICKER>]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..cli import emit
from ..contract import TraceEntry
from . import Position
from .sizer import load_long_book_from_json

TOOL_JACCARD = "tools/thematic_portfolio/ensemble_overlap.py::jaccard"
TOOL_TRIANGULATION = "tools/thematic_portfolio/ensemble_overlap.py::triangulation_rank"
TOOL_CRITIC_TRIGGER = "tools/thematic_portfolio/ensemble_overlap.py::critic_trigger_context"

DEFAULT_TOP_K = 10
ENSEMBLE_FUNDS = ("altimeter", "coatue", "light_street")


def _tickers(book: list[Position]) -> set[str]:
    return {p.ticker for p in book}


def _top_k_tickers(book: list[Position], k: int) -> set[str]:
    """Return the tickers of the top-K positions by value_usd."""
    if k <= 0:
        raise ValueError(f"k must be positive; got {k}")
    sorted_book = sorted(book, key=lambda p: p.value_usd, reverse=True)
    return {p.ticker for p in sorted_book[:k]}


def compute_jaccard(
    sa_lp_book: list[Position],
    subagent_book: list[Position],
) -> TraceEntry:
    """M1 — position-set Jaccard between SA LP and the subagent's recommended book.

    Args:
        sa_lp_book: SA LP's current long book.
        subagent_book: subagent's currently-held thematic positions
            (or recommended positions for a counterfactual calibration).

    Returns:
        TraceEntry with output keys: ``jaccard``, ``intersection_count``,
        ``union_count``, ``sa_lp_count``, ``subagent_count``,
        ``intersection_tickers``, ``sa_lp_only_tickers``,
        ``subagent_only_tickers``, ``passes_m1_threshold`` (true when
        jaccard ≥ 0.85 per session-2 #3 M1 spec).
    """
    sa = _tickers(sa_lp_book)
    sub = _tickers(subagent_book)
    intersection = sa & sub
    union = sa | sub
    jaccard = (len(intersection) / len(union)) if union else 0.0

    return TraceEntry(
        tool=TOOL_JACCARD,
        inputs={
            "sa_lp_count": len(sa),
            "subagent_count": len(sub),
        },
        output={
            "jaccard": jaccard,
            "intersection_count": len(intersection),
            "union_count": len(union),
            "sa_lp_count": len(sa),
            "subagent_count": len(sub),
            "intersection_tickers": sorted(intersection),
            "sa_lp_only_tickers": sorted(sa - sub),
            "subagent_only_tickers": sorted(sub - sa),
            "passes_m1_threshold": jaccard >= 0.85,
        },
    )


def compute_ensemble_triangulation_rank(
    sa_lp_book: list[Position],
    ensemble_books: dict[str, list[Position]],
    top_k: int = DEFAULT_TOP_K,
) -> TraceEntry:
    """M3 — rank-based ensemble triangulation.

    For SA LP's top-K positions, what fraction also appear in the UNION of
    top-K positions across the ensemble funds? Rank-based per session-2 #4
    (notional weighting drowns the smallest fund).

    Args:
        sa_lp_book: SA LP's current long book.
        ensemble_books: dict keyed by fund name (subset of
            ``{"altimeter", "coatue", "light_street"}``) → that fund's
            current long book.
        top_k: how many top-by-value positions to consider per fund.
            Default 10.

    Returns:
        TraceEntry with output keys: ``overlap_pct``, ``passes_m3_threshold``
        (true when overlap_pct ≥ 0.5 per session-2 #3 M3), per-fund overlap
        breakdown, intersection + sa_lp_only at the top-K level.
    """
    unknown = set(ensemble_books.keys()) - set(ENSEMBLE_FUNDS)
    if unknown:
        raise ValueError(
            f"unknown ensemble fund(s): {unknown}; expected subset of {ENSEMBLE_FUNDS}"
        )
    if not sa_lp_book:
        raise ValueError("sa_lp_book must be non-empty")

    sa_top = _top_k_tickers(sa_lp_book, top_k)
    ensemble_top_union: set[str] = set()
    per_fund: dict[str, dict] = {}
    for fund, book in ensemble_books.items():
        if not book:
            per_fund[fund] = {
                "top_k_tickers": [],
                "overlap_with_sa_lp_top_k": [],
                "overlap_count": 0,
                "overlap_pct_of_sa_lp_top_k": 0.0,
            }
            continue
        fund_top = _top_k_tickers(book, top_k)
        ensemble_top_union |= fund_top
        overlap = sa_top & fund_top
        per_fund[fund] = {
            "top_k_tickers": sorted(fund_top),
            "overlap_with_sa_lp_top_k": sorted(overlap),
            "overlap_count": len(overlap),
            "overlap_pct_of_sa_lp_top_k": len(overlap) / len(sa_top) if sa_top else 0.0,
        }

    intersection = sa_top & ensemble_top_union
    overlap_pct = len(intersection) / len(sa_top) if sa_top else 0.0

    return TraceEntry(
        tool=TOOL_TRIANGULATION,
        inputs={
            "sa_lp_long_book_n_positions": len(sa_lp_book),
            "ensemble_funds": sorted(ensemble_books.keys()),
            "top_k": top_k,
        },
        output={
            "overlap_pct": overlap_pct,
            "passes_m3_threshold": overlap_pct >= 0.5,
            "sa_lp_top_k_tickers": sorted(sa_top),
            "ensemble_top_k_union_tickers": sorted(ensemble_top_union),
            "intersection_tickers": sorted(intersection),
            "sa_lp_only_at_top_k": sorted(sa_top - ensemble_top_union),
            "per_fund": per_fund,
            "weighting": "rank_based",
        },
    )


def compute_critic_trigger_context(
    ticker: str,
    sa_lp_latest_tickers: set[str],
    sa_lp_prior_tickers: set[str],
    ensemble_latest: dict[str, set[str]],
    ensemble_prior: dict[str, set[str]],
) -> TraceEntry:
    """Per-position critic-trigger context per session-2 design change #5.

    Args:
        ticker: position to evaluate.
        sa_lp_latest_tickers: ticker set from SA LP's current 13F long book.
        sa_lp_prior_tickers: ticker set from SA LP's prior 13F long book.
        ensemble_latest: dict keyed by ``{"altimeter", "coatue", "light_street"}`` →
            that fund's current 13F long-book ticker set.
        ensemble_prior: same shape, prior 13F. Funds present in the latest
            but missing from the prior dict are treated as having had an
            empty prior book (e.g. Light Street Photon when it first files).

    Returns:
        TraceEntry with output keys: ``trigger_rule`` (one of
        ``ensemble_disagreement`` / ``sa_lp_doubling_down_vs_consensus_exit`` /
        ``non_consensus_sa_lp_solo`` / ``none``), ``ensemble_holds``,
        ``ensemble_exits``, ``conviction_tier``, ``sa_lp_added_this_quarter``,
        ``context_summary``.

    Raises:
        ValueError: ensemble_latest contains a fund not in ENSEMBLE_FUNDS.
    """
    unknown = set(ensemble_latest.keys()) - set(ENSEMBLE_FUNDS)
    if unknown:
        raise ValueError(
            f"unknown ensemble fund(s) in latest: {unknown}; "
            f"expected subset of {ENSEMBLE_FUNDS}"
        )

    ensemble_holds = sorted(
        f for f, ts in ensemble_latest.items() if ticker in ts
    )
    ensemble_exits = sorted(
        f
        for f in ensemble_latest.keys()
        if (
            ticker in ensemble_prior.get(f, set())
            and ticker not in ensemble_latest.get(f, set())
        )
    )
    sa_lp_holds = ticker in sa_lp_latest_tickers
    sa_lp_added_this_quarter = (
        ticker in sa_lp_latest_tickers and ticker not in sa_lp_prior_tickers
    )

    if ensemble_exits and ensemble_holds:
        trigger_rule = "ensemble_disagreement"
        context_summary = (
            f"{ticker} held by {ensemble_holds}; exited by {ensemble_exits} "
            "this quarter — ensemble is split. Critic panel evaluates whether "
            "the exits read off-thesis change SA LP did not act on."
        )
    elif ensemble_exits and not ensemble_holds:
        if sa_lp_added_this_quarter:
            trigger_rule = "sa_lp_doubling_down_vs_consensus_exit"
            context_summary = (
                f"{ticker}: all ensemble funds exited this quarter ({ensemble_exits}); "
                "SA LP NEWLY added. High-conviction non-consensus position — critic "
                "panel evaluates whether SA LP has private signal or is alone in error."
            )
        else:
            trigger_rule = "non_consensus_sa_lp_solo"
            context_summary = (
                f"{ticker}: all ensemble funds exited this quarter ({ensemble_exits}); "
                "SA LP continues to hold (not newly added). Non-consensus carry — "
                "critic panel evaluates whether SA LP's thesis still holds."
            )
    elif not ensemble_holds:
        trigger_rule = "non_consensus_sa_lp_solo"
        context_summary = (
            f"{ticker}: no ensemble fund holds; SA LP holds. Non-consensus thesis — "
            "critic panel evaluates baseline."
        )
    else:
        trigger_rule = "none"
        context_summary = (
            f"{ticker}: held by SA LP and {ensemble_holds}; no recent ensemble exits. "
            "Standard consensus position — critic panel runs at baseline weight."
        )

    return TraceEntry(
        tool=TOOL_CRITIC_TRIGGER,
        inputs={
            "ticker": ticker,
            "ensemble_funds": sorted(ensemble_latest.keys()),
        },
        output={
            "ticker": ticker,
            "trigger_rule": trigger_rule,
            "ensemble_holds": ensemble_holds,
            "ensemble_exits": ensemble_exits,
            "conviction_tier": "boost" if ensemble_holds else "sa_lp_only",
            "sa_lp_holds": sa_lp_holds,
            "sa_lp_added_this_quarter": sa_lp_added_this_quarter,
            "context_summary": context_summary,
        },
    )


def _load_books(args: argparse.Namespace) -> tuple[
    list[Position],
    dict[str, list[Position]],
    list[Position] | None,
    dict[str, list[Position]],
]:
    """Load whichever subset of 13F books the CLI was given."""
    sa_lp_book = load_long_book_from_json(args.sa_lp)
    ensemble_latest: dict[str, list[Position]] = {}
    if args.altimeter:
        ensemble_latest["altimeter"] = load_long_book_from_json(args.altimeter)
    if args.coatue:
        ensemble_latest["coatue"] = load_long_book_from_json(args.coatue)
    if args.light_street:
        ensemble_latest["light_street"] = load_long_book_from_json(args.light_street)

    sa_lp_prior_book = (
        load_long_book_from_json(args.sa_lp_prior) if args.sa_lp_prior else None
    )
    ensemble_prior: dict[str, list[Position]] = {}
    if args.altimeter_prior:
        ensemble_prior["altimeter"] = load_long_book_from_json(args.altimeter_prior)
    if args.coatue_prior:
        ensemble_prior["coatue"] = load_long_book_from_json(args.coatue_prior)
    if args.light_street_prior:
        ensemble_prior["light_street"] = load_long_book_from_json(args.light_street_prior)
    return sa_lp_book, ensemble_latest, sa_lp_prior_book, ensemble_prior


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.ensemble_overlap",
        description=(
            "Ensemble overlap + critic-trigger context. By default runs M3 "
            "rank-based triangulation. Add --subagent for M1 Jaccard. Add "
            "--position-trigger TICKER for per-position critic-trigger context."
        ),
    )
    p.add_argument("--sa-lp", type=Path, required=True, help="SA LP latest 13F long book.")
    p.add_argument("--altimeter", type=Path, default=None)
    p.add_argument("--coatue", type=Path, default=None)
    p.add_argument("--light-street", type=Path, default=None)
    p.add_argument("--sa-lp-prior", type=Path, default=None, help="SA LP prior 13F long book.")
    p.add_argument("--altimeter-prior", type=Path, default=None)
    p.add_argument("--coatue-prior", type=Path, default=None)
    p.add_argument("--light-street-prior", type=Path, default=None)
    p.add_argument(
        "--subagent",
        type=Path,
        default=None,
        help="Subagent's current thematic book — required for M1 Jaccard.",
    )
    p.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"Top-K rank for M3 triangulation (default {DEFAULT_TOP_K}).",
    )
    p.add_argument(
        "--jaccard-only",
        action="store_true",
        help="Emit M1 only (requires --subagent).",
    )
    p.add_argument(
        "--position-trigger",
        type=str,
        default=None,
        metavar="TICKER",
        help=(
            "Emit per-position critic-trigger context for TICKER. Requires "
            "--sa-lp-prior + the relevant ensemble-prior flags."
        ),
    )
    args = p.parse_args()

    sa_lp_book, ensemble_latest, sa_lp_prior_book, ensemble_prior = _load_books(args)

    if args.position_trigger:
        if sa_lp_prior_book is None:
            p.error("--position-trigger requires --sa-lp-prior")
        emit(
            compute_critic_trigger_context(
                ticker=args.position_trigger,
                sa_lp_latest_tickers=_tickers(sa_lp_book),
                sa_lp_prior_tickers=_tickers(sa_lp_prior_book),
                ensemble_latest={f: _tickers(b) for f, b in ensemble_latest.items()},
                ensemble_prior={f: _tickers(b) for f, b in ensemble_prior.items()},
            )
        )
        return

    if args.jaccard_only:
        if not args.subagent:
            p.error("--jaccard-only requires --subagent")
        subagent_book = load_long_book_from_json(args.subagent)
        emit(compute_jaccard(sa_lp_book=sa_lp_book, subagent_book=subagent_book))
        return

    if not ensemble_latest:
        p.error(
            "M3 triangulation requires at least one of "
            "--altimeter / --coatue / --light-street"
        )
    emit(
        compute_ensemble_triangulation_rank(
            sa_lp_book=sa_lp_book,
            ensemble_books=ensemble_latest,
            top_k=args.top_k,
        )
    )


if __name__ == "__main__":
    main()
