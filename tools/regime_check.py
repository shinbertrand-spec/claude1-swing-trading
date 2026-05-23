"""Three-level regime check per ``swing-regime-playbook.md``.

Composes :mod:`tools.trend_template` against:

    Level 1 — broad market (SPY or QQQ), skip RS criterion (7 max)
    Level 2 — sector ETF (XLK, XLE, ...), skip RS criterion (7 max)
    Level 3 — candidate stock, full 8 criteria

Returns the regime classification + risk-budget multiplier per the
playbook's table:

    7/7   stage_2_confirmed     multiplier 1.0
    5-6/7 stage_2_weakening     multiplier 0.75
    3-4/7 stage_3_transitional  multiplier 0.5
    0-2/7 stage_4               multiplier 0.0

Stage 4 in the broad market is a **circuit breaker** — no new entries.

CLI::

    uv run python -m tools.regime_check AAPL --sector XLK
"""
from __future__ import annotations

import argparse

from .cli import emit
from .contract import TraceEntry
from .trend_template import compute_from_ticker as tt_from_ticker

TOOL = "tools/regime_check.py"


def classify_broad(passes_7: int) -> tuple[str, float]:
    """Map broad-market 0-7 score → (stage_class, multiplier).

    Sector uses the same mapping; candidate is reported on its own scale.
    """
    if passes_7 >= 7:
        return "stage_2_confirmed", 1.0
    if passes_7 >= 5:
        return "stage_2_weakening", 0.75
    if passes_7 >= 3:
        return "stage_3_transitional", 0.5
    return "stage_4", 0.0


def compute(
    candidate_ticker: str,
    sector_etf: str | None = None,
    broad_ticker: str = "SPY",
    candidate_rs_rating: int | None = None,
) -> TraceEntry:
    """Run the three-level check; return composed verdict.

    Args:
        candidate_ticker: the stock being evaluated.
        sector_etf: SPDR sector ETF (XLK/XLE/etc.). If ``None``, Level 2
            is skipped — flagged in output as ``sector_skipped``.
        broad_ticker: SPY (default) or QQQ.
        candidate_rs_rating: optional IBD RS rating for the candidate.
    """
    broad = tt_from_ticker(broad_ticker, include_rs=False)
    broad_passes = broad.output["trend_template_passes"]
    broad_class, regime_mult = classify_broad(broad_passes)

    sector_result: dict | None = None
    if sector_etf is not None:
        sector = tt_from_ticker(sector_etf, include_rs=False)
        sector_passes = sector.output["trend_template_passes"]
        sector_class, _ = classify_broad(sector_passes)
        sector_qualifies = sector_passes >= 5
        sector_result = {
            "ticker": sector_etf,
            "trend_template_passes": sector_passes,
            "stage_class": sector_class,
            "qualifies_for_long": sector_qualifies,
            "stage": sector.output["stage"],
        }
    candidate = tt_from_ticker(
        candidate_ticker, include_rs=True, rs_rating=candidate_rs_rating
    )

    # The candidate qualifies for entry per playbook only when:
    # broad >= stage_2 (any) AND sector qualifies (>=5/7) AND candidate trend
    # template passes >= 6 (per stage-2 derivation in trend_template).
    candidate_qualifies = (
        regime_mult > 0
        and (sector_etf is None or sector_result["qualifies_for_long"])
        and candidate.output["stage"] == 2
    )

    return TraceEntry(
        tool=TOOL,
        inputs={
            "candidate_ticker": candidate_ticker,
            "sector_etf": sector_etf,
            "broad_ticker": broad_ticker,
            "candidate_rs_rating": candidate_rs_rating,
        },
        output={
            "broad_market": {
                "ticker": broad_ticker,
                "trend_template_passes": broad_passes,
                "stage_class": broad_class,
                "stage": broad.output["stage"],
            },
            "sector": sector_result,
            "candidate": {
                "ticker": candidate_ticker,
                "trend_template_passes": candidate.output["trend_template_passes"],
                "stage": candidate.output["stage"],
                "criteria": candidate.output["criteria"],
            },
            "regime_multiplier": regime_mult,
            "candidate_qualifies_for_entry": candidate_qualifies,
            "circuit_breaker_stage_4": broad_class == "stage_4",
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.regime_check",
        description="3-level regime check per swing-regime-playbook.",
    )
    p.add_argument("ticker", help="Candidate ticker")
    p.add_argument("--sector", help="Sector ETF (e.g. XLK)", default=None)
    p.add_argument("--broad", default="SPY", help="Broad-market index (SPY|QQQ)")
    p.add_argument("--rs", type=int, default=None, help="Candidate IBD RS rating 1-99")
    args = p.parse_args()
    emit(
        compute(
            candidate_ticker=args.ticker,
            sector_etf=args.sector,
            broad_ticker=args.broad,
            candidate_rs_rating=args.rs,
        )
    )


if __name__ == "__main__":
    main()
