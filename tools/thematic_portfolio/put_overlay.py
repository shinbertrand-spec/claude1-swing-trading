"""Put-overlay tracker (Loop 3) — short-overlay sizing recommendation.

Per [[swing-thematic-portfolio-subagent-research]] § Loop 3 + Q9 design:
when Loop 1 raises ``short_overlay_bias_flag.fired = true``, this module
turns that flag into a concrete recommendation Bertrand can act on:

1. **Primary (cash-raise leg):** reduce thematic long exposure by a percentage
   derived from SA LP's barbell ratio (put_complex_notional / long_book_value).
   Capped at 50% reduction per the design — full-mirror replication of
   SA LP's barbell would push leverage way beyond a retail book's capacity.

2. **Secondary (portfolio-insurance leg, optional):** 1-2% of total portfolio
   in NVDA OTM puts, 60-90 day expiry. NVDA-only per Week 1c verification
   ([[swing-thematic-portfolio-week-1c-tiger-verification]]): SMH OTM put
   spreads measured at 6.3-12.6% on yfinance public NMS — fails the spec's
   5% spread-quality threshold. NVDA OTM put spreads measured at 1.3-2.2% —
   passes. Hard refusal on SMH in any form.

3. **Cross-reference signal:** Light Street's chip-long deltas. SA LP shorts
   chips; Light Street is broadly chip-bullish. The magnitude of Light Street's
   chip-long allocation indicates the contrarian conviction SA LP is taking
   against consensus. Higher Light Street chip exposure → SA LP's bearish
   chip thesis is more contrarian → higher conviction-divergence flag for
   Bertrand's review.

This module does NOT place trades. It emits a structured TraceEntry the
orchestrator can pass back to Loop 1 (as `loop3_recommendation` block) or
surface directly to Bertrand. v1 is advisory only; Bertrand executes the
cash-raise + (optional) NVDA-puts manually.

## Hard refusals encoded

- SMH OTM puts in any form (Week 1c verification — spread quality)
- Any other put underlying beyond NVDA (out of scope; if Bertrand wants
  TSM/AVGO put insurance, that's a v2 conversation after spread quality
  is measured)
- Portfolio-insurance allocation > 2% of total (Q9 hard cap)
- Cash-raise reduction > 50% of thematic long book (retail leverage cap)
- Recommendation to replicate SA LP's institutional-scale put complex
  (impossible at retail size; instructed to refuse)

## CLI

::

    uv run python -m tools.thematic_portfolio.put_overlay \\
        --sa-lp-long-book <path>.json \\
        --sa-lp-put-complex <path>.json \\
        --light-street-long-book <path>.json \\
        --thematic-allocation 25 \\
        [--rationale-from-loop1 "<short_overlay_bias_flag.rationale>"]
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..cli import emit
from ..contract import TraceEntry
from . import Position
from .sizer import load_long_book_from_json

TOOL = "tools/thematic_portfolio/put_overlay.py"

# Chip cluster — defined as the underlying-set in SA LP's Q1 2026 put complex
# plus the standard chip-leader peers. Used to detect Light Street's chip-long
# exposure (their bullish-chip signal contrast against SA LP's bearish thesis).
# Per session-2 design change #6 these are illustrative — refresh against
# live data each cycle rather than treating as a frozen contract.
CHIP_CLUSTER_TICKERS = frozenset({
    "NVDA",
    "AVGO",
    "AMD",
    "MU",
    "TSM",
    "ASML",
    "INTC",
    "ORCL",
    "SMH",
    "QCOM",
    "MRVL",
    "ARM",
})

# Operational caps per design
MAX_CASH_RAISE_PCT_OF_LONG_BOOK = 50.0
MAX_NVDA_PUTS_PCT_OF_TOTAL = 2.0
NVDA_PUTS_DEFAULT_PCT_OF_TOTAL = 1.5
NVDA_OTM_PUTS_EXPIRY_RANGE = "60-90 days"

# Week 1c verification — measured spreads (yfinance public NMS, 2026-05-25)
SPREAD_QUALITY_THRESHOLD_PCT = 5.0
NVDA_SPREAD_RANGE = "1.3-2.2%"
SMH_SPREAD_RANGE = "6.3-12.6%"


@dataclass
class Loop3Recommendation:
    """The output shape Loop 1's `short_overlay_bias_flag` block consumes.

    Mirrors the Loop 1 prompt's Output Contract § short_overlay_bias_flag
    sub-block, so the orchestrator can drop this dict in directly.
    """

    primary_recommendation: dict
    secondary_recommendation: dict | None
    refused_secondary_recommendations: list[dict] = field(default_factory=list)
    sa_lp_barbell_ratio: float = 0.0
    light_street_chip_exposure_pct: float = 0.0
    rationale_summary: str = ""
    source_refs: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "primary_recommendation": self.primary_recommendation,
            "secondary_recommendation": self.secondary_recommendation,
            "refused_secondary_recommendations": self.refused_secondary_recommendations,
            "sa_lp_barbell_ratio": self.sa_lp_barbell_ratio,
            "light_street_chip_exposure_pct": self.light_street_chip_exposure_pct,
            "rationale_summary": self.rationale_summary,
            "source_refs": self.source_refs,
        }


def _book_value(positions: list[Position]) -> float:
    return float(sum(p.value_usd for p in positions))


def _chip_cluster_value(positions: list[Position]) -> float:
    return float(sum(p.value_usd for p in positions if p.ticker.upper() in CHIP_CLUSTER_TICKERS))


def _load_positions(path: Path) -> list[Position]:
    """Load a JSON book (long or put-complex). put-complex JSON is structurally
    identical to long-book JSON — both are lists of position dicts with
    ``ticker``/``issuer_name``/``value_usd``/``cusip``. The thirteen_f fetcher
    writes them in the same shape with the leg distinguished by filename.
    """
    return load_long_book_from_json(path)


def compute(
    sa_lp_long_book: list[Position],
    sa_lp_put_complex: list[Position],
    light_street_long_book: list[Position],
    thematic_allocation_pct: float,
    *,
    rationale_from_loop1: str | None = None,
    include_portfolio_insurance: bool = True,
) -> TraceEntry:
    """Compute the Loop 3 short-overlay recommendation.

    Args:
        sa_lp_long_book: SA LP's current long-book positions.
        sa_lp_put_complex: SA LP's current put-complex positions (notional
            values, not premium paid — 13F infotable convention).
        light_street_long_book: Light Street's current long book (used for
            the cross-reference chip-exposure signal only).
        thematic_allocation_pct: current Loop 5 phase allocation as %.
            Must be 10, 15, or 25 to align with the orchestrator's checks.
        rationale_from_loop1: optional pass-through of Loop 1's
            `short_overlay_bias_flag.rationale` for downstream
            attribution.
        include_portfolio_insurance: if False, the secondary NVDA-puts
            leg is omitted. Useful when Bertrand wants the cash-raise
            recommendation only (e.g., paper-account doesn't have options
            quote permissions; see Week 1c verification).

    Returns:
        TraceEntry whose output is a :class:`Loop3Recommendation` dict
        plus aggregate stats (long_book_value_usd, put_complex_notional,
        chip_cluster_value at Light Street).

    Raises:
        ValueError: empty long book, empty put complex, invalid allocation,
            any position with non-positive value_usd, or include_portfolio_insurance
            True but the spread-quality precondition violated (defensive —
            should never fire given hardcoded NVDA-only path).
    """
    if not sa_lp_long_book:
        raise ValueError("sa_lp_long_book must be non-empty")
    if not sa_lp_put_complex:
        raise ValueError(
            "sa_lp_put_complex must be non-empty — Loop 3 fires when SA LP has "
            "an active put-overlay; an empty put complex means no Loop 3 signal"
        )
    if thematic_allocation_pct not in (10.0, 15.0, 25.0):
        raise ValueError(
            f"thematic_allocation_pct must be 10, 15, or 25; got {thematic_allocation_pct}"
        )
    for label, book in (
        ("sa_lp_long_book", sa_lp_long_book),
        ("sa_lp_put_complex", sa_lp_put_complex),
        ("light_street_long_book", light_street_long_book),
    ):
        if any(p.value_usd <= 0 for p in book):
            bad = [p.ticker for p in book if p.value_usd <= 0]
            raise ValueError(f"{label} contains non-positive value_usd: {bad}")

    long_book_value = _book_value(sa_lp_long_book)
    put_complex_notional = _book_value(sa_lp_put_complex)
    ls_book_value = _book_value(light_street_long_book) if light_street_long_book else 0.0
    ls_chip_value = _chip_cluster_value(light_street_long_book) if light_street_long_book else 0.0

    sa_lp_barbell_ratio = put_complex_notional / long_book_value if long_book_value > 0 else 0.0
    ls_chip_exposure_pct = (ls_chip_value / ls_book_value * 100.0) if ls_book_value > 0 else 0.0

    # ----------------------------------------------------------------
    # Primary recommendation — cash-raise leg
    # ----------------------------------------------------------------
    # SA LP's barbell ratio (put_complex / long_book) is the raw signal.
    # In retail-sized portfolio terms, mirror it as "reduce thematic long
    # by ratio × 100%". But cap at 50% — full mirror would imply enormous
    # short leverage that paper-only retail can't replicate.
    raw_cash_raise_pct = sa_lp_barbell_ratio * 100.0
    cash_raise_pct = min(raw_cash_raise_pct, MAX_CASH_RAISE_PCT_OF_LONG_BOOK)
    cash_raise_cap_bound = cash_raise_pct >= MAX_CASH_RAISE_PCT_OF_LONG_BOOK

    primary_recommendation: dict = {
        "type": "cash_raise",
        "pct_reduction_of_thematic_long_book": cash_raise_pct,
        "raw_pct_implied_by_sa_lp_barbell": raw_cash_raise_pct,
        "cap_bound": cash_raise_cap_bound,
        "rationale": (
            f"SA LP barbell ratio = ${put_complex_notional/1e9:.2f}B put complex / "
            f"${long_book_value/1e9:.2f}B long book = {sa_lp_barbell_ratio:.2f}× "
            f"({raw_cash_raise_pct:.0f}% put-overlay-vs-long). Mirroring at "
            f"min(raw, {MAX_CASH_RAISE_PCT_OF_LONG_BOOK:.0f}%) cap → "
            f"reduce thematic long exposure by {cash_raise_pct:.1f}%."
        ),
    }

    # ----------------------------------------------------------------
    # Secondary recommendation — portfolio-insurance leg (optional)
    # ----------------------------------------------------------------
    secondary_recommendation: dict | None = None
    refused: list[dict] = []

    if include_portfolio_insurance:
        nvda_pct = min(NVDA_PUTS_DEFAULT_PCT_OF_TOTAL, MAX_NVDA_PUTS_PCT_OF_TOTAL)
        if nvda_pct > MAX_NVDA_PUTS_PCT_OF_TOTAL:
            # Defensive — should never fire
            raise ValueError(
                f"NVDA puts pct {nvda_pct} exceeds hard cap {MAX_NVDA_PUTS_PCT_OF_TOTAL}"
            )
        secondary_recommendation = {
            "type": "nvda_otm_puts",
            "pct_of_total_portfolio": nvda_pct,
            "expiry_range": NVDA_OTM_PUTS_EXPIRY_RANGE,
            "spread_quality_check_passed": True,
            "spread_measured": NVDA_SPREAD_RANGE,
            "spread_threshold_pct": SPREAD_QUALITY_THRESHOLD_PCT,
            "rationale": (
                f"NVDA OTM puts sized at {nvda_pct:.1f}% of total portfolio, "
                f"{NVDA_OTM_PUTS_EXPIRY_RANGE} expiry. Per Week 1c verification "
                f"({NVDA_SPREAD_RANGE} spread on yfinance public NMS) — passes "
                f"the {SPREAD_QUALITY_THRESHOLD_PCT}% spread-quality threshold."
            ),
        }

    # Always emit the SMH refusal — the orchestrator surfaces this so a
    # future Bertrand decision-aid doesn't accidentally re-add SMH.
    refused.append(
        {
            "type": "smh_otm_puts",
            "reason": (
                f"Per Week 1c verification, SMH OTM put spreads = {SMH_SPREAD_RANGE} "
                f"across all OTM strikes via yfinance public NMS — exceeds the "
                f"{SPREAD_QUALITY_THRESHOLD_PCT}% spread-quality threshold."
            ),
            "spread_threshold_pct": SPREAD_QUALITY_THRESHOLD_PCT,
            "spread_measured": SMH_SPREAD_RANGE,
        }
    )
    refused.append(
        {
            "type": "sa_lp_put_complex_replication",
            "reason": (
                "SA LP's put complex ($8.46B notional Q1 2026) is institutional-only "
                "by strike/expiry/theta/margin structure. Retail-sized replication "
                "is not feasible and not in v1 scope. Cash-raise primary + 1-2% "
                "NVDA-puts insurance secondary is the retail expression."
            ),
        }
    )

    # ----------------------------------------------------------------
    # Rationale summary
    # ----------------------------------------------------------------
    rationale_bits: list[str] = []
    if rationale_from_loop1:
        rationale_bits.append(f"Loop 1 trigger: {rationale_from_loop1.strip()}.")
    rationale_bits.append(
        f"SA LP barbell {sa_lp_barbell_ratio:.2f}× (long ${long_book_value/1e9:.2f}B "
        f"/ puts ${put_complex_notional/1e9:.2f}B notional)."
    )
    if ls_book_value > 0:
        rationale_bits.append(
            f"Light Street cross-reference: chip-cluster exposure "
            f"${ls_chip_value/1e9:.2f}B / ${ls_book_value/1e9:.2f}B long book "
            f"({ls_chip_exposure_pct:.1f}%) — Light Street is chip-bullish; "
            f"SA LP's bearish chip thesis is contrarian to this signal."
        )
    rationale_summary = " ".join(rationale_bits)

    # ----------------------------------------------------------------
    # Source refs
    # ----------------------------------------------------------------
    source_refs: list[dict] = [
        {
            "kind": "sa_lp_long_book_path",
            "n_positions": len(sa_lp_long_book),
            "total_value_usd": long_book_value,
        },
        {
            "kind": "sa_lp_put_complex_path",
            "n_positions": len(sa_lp_put_complex),
            "total_notional_usd": put_complex_notional,
        },
    ]
    if light_street_long_book:
        source_refs.append(
            {
                "kind": "light_street_long_book_path",
                "n_positions": len(light_street_long_book),
                "total_value_usd": ls_book_value,
                "chip_cluster_value_usd": ls_chip_value,
            }
        )
    source_refs.append(
        {
            "kind": "week_1c_verification",
            "vault_note": (
                "swing-thematic-portfolio-week-1c-tiger-verification"
            ),
            "summary": (
                "NVDA OTM puts pass spread-quality threshold; SMH refuses."
            ),
        }
    )

    rec = Loop3Recommendation(
        primary_recommendation=primary_recommendation,
        secondary_recommendation=secondary_recommendation,
        refused_secondary_recommendations=refused,
        sa_lp_barbell_ratio=sa_lp_barbell_ratio,
        light_street_chip_exposure_pct=ls_chip_exposure_pct,
        rationale_summary=rationale_summary,
        source_refs=source_refs,
    )

    return TraceEntry(
        tool=TOOL,
        inputs={
            "n_sa_lp_long_positions": len(sa_lp_long_book),
            "n_sa_lp_put_positions": len(sa_lp_put_complex),
            "n_light_street_long_positions": len(light_street_long_book),
            "thematic_allocation_pct": thematic_allocation_pct,
            "include_portfolio_insurance": include_portfolio_insurance,
        },
        output={
            "loop3_recommendation": rec.to_dict(),
            "long_book_value_usd": long_book_value,
            "put_complex_notional_usd": put_complex_notional,
            "light_street_long_book_value_usd": ls_book_value,
            "light_street_chip_cluster_value_usd": ls_chip_value,
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.put_overlay",
        description=(
            "Loop 3 short-overlay recommendation. Fires when Loop 1 sets "
            "short_overlay_bias_flag.fired=True. Reads SA LP's long + put "
            "13F slices + Light Street's long book + emits a structured "
            "cash-raise + NVDA-puts recommendation."
        ),
    )
    p.add_argument("--sa-lp-long-book", type=Path, required=True)
    p.add_argument("--sa-lp-put-complex", type=Path, required=True)
    p.add_argument(
        "--light-street-long-book",
        type=Path,
        default=None,
        help="Optional — Light Street long book for the chip-cross-reference signal.",
    )
    p.add_argument(
        "--thematic-allocation",
        type=float,
        required=True,
        dest="thematic_allocation_pct",
        help="Current Loop 5 phase allocation: 10, 15, or 25.",
    )
    p.add_argument(
        "--rationale-from-loop1",
        type=str,
        default=None,
        help="Pass-through of Loop 1's short_overlay_bias_flag rationale.",
    )
    p.add_argument(
        "--no-portfolio-insurance",
        action="store_true",
        help="Omit the secondary NVDA-puts leg (cash-raise only).",
    )
    args = p.parse_args()
    sa_lp_long = _load_positions(args.sa_lp_long_book)
    sa_lp_puts = _load_positions(args.sa_lp_put_complex)
    ls_long: list[Position] = (
        _load_positions(args.light_street_long_book)
        if args.light_street_long_book
        else []
    )
    emit(
        compute(
            sa_lp_long_book=sa_lp_long,
            sa_lp_put_complex=sa_lp_puts,
            light_street_long_book=ls_long,
            thematic_allocation_pct=args.thematic_allocation_pct,
            rationale_from_loop1=args.rationale_from_loop1,
            include_portfolio_insurance=not args.no_portfolio_insurance,
        )
    )


if __name__ == "__main__":
    main()
