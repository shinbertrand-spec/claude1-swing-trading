"""End-to-end smoke test for the auto-paper pipeline with lever D wired.

Constructs a synthetic deployable-setup CandidateInput and runs
place_candidate(dry_run=True) against the live Tiger paper account. The
output should show the regime-conditional sizing multiplier applied to
the shares (today: stage_2_weakening → 0.75×).

Run:
    uv run python scripts/smoke_place_candidate.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.auto_paper.pipeline import CandidateInput, place_candidate


def main() -> None:
    # Use dual_ma_trend_following — one of the 3 currently-deployable
    # KIND_REGISTRY setups. Picking a synthetic ticker (NVDA) and
    # synthetic prices that look plausible.
    cand = CandidateInput(
        ticker="NVDA",
        setup_type="dual_ma_trend_following",
        setup_grade="B",
        pivot_price=140.00,
        limit_price=140.28,    # +0.2%
        stop_price=132.00,     # ~6% below
        target_price=None,
        shares=100,            # pre-regime sizing
        sector_etf="XLK",
    )
    print(f"# Pre-pipeline candidate: {cand.shares} shares of {cand.ticker} @ ${cand.limit_price:.2f}")
    print(f"#   cost: ${cand.shares * cand.limit_price:,.2f}")
    print()

    result = place_candidate(cand, dry_run=True)
    print(f"# Pipeline result")
    print(f"#   status: {result.status}")
    print(f"#   reason: {result.reason}")
    if result.cost_estimate_usd is not None:
        print(f"#   cost_estimate_usd: ${result.cost_estimate_usd:,.2f}")
    # Note: result.reason includes the post-regime share count. If the
    # live SPY regime is stage_2_weakening (0.75x), expect ~75 shares.
    # If stage_2_confirmed (1.0x), expect 100. If stage_3_transitional
    # (0.5x), expect 50. If stage_4 (0.0x), expect status="rejected".


if __name__ == "__main__":
    main()
