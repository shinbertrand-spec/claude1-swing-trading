"""MAGNA score for EP setup classification (per ``swing-earnings-pivot.md``).

Five criteria, each True/False:

* **M** — Massive earnings: 100%+ EPS growth OR 100%+ sales growth YoY
* **A** — After-hours gap up: ≥4% on ≥100k premarket volume
* **G** — Gap up confirmed in regular session: open above prev close + sustains
* **N** — Neglected: low prior-rally per :mod:`tools.prior_rally_pct`
* **A** — Analyst upgrades: at least one upgrade in last N days

Score = count of True. 5/5 is a Golden EP candidate; ``ep_grade.py`` then
applies gap-band + intraday-expansion filters to upgrade or downgrade.

Pure compute — caller supplies the booleans (or values for M; the tool
checks the threshold). No data fetch.

CLI::

    uv run python -m tools.magna_score --eps-yoy 3.92 --sales-yoy 1.74 \\
        --after-hours-gap-pct 0.07 --premarket-vol 380000 \\
        --gap-confirmed --neglected --analyst-upgrades
"""
from __future__ import annotations

import argparse

from .cli import emit
from .contract import TraceEntry

TOOL = "tools/magna_score.py"

# Per swing-earnings-pivot.md "MAGNA criteria" section.
MASSIVE_GROWTH_THRESHOLD = 1.00   # 100% YoY
AFTER_HOURS_GAP_THRESHOLD = 0.04  # 4%
AFTER_HOURS_VOL_THRESHOLD = 100_000


def compute(
    eps_yoy_growth: float | None,
    sales_yoy_growth: float | None,
    after_hours_gap_pct: float | None,
    premarket_volume_shares: int | None,
    gap_confirmed_regular_session: bool,
    neglected: bool,
    analyst_upgrades: bool,
) -> TraceEntry:
    """Score 0-5 + per-criterion breakdown.

    Args:
        eps_yoy_growth: decimal (e.g. 3.92 = 392%). ``None`` if unknown.
        sales_yoy_growth: decimal. ``None`` if unknown.
        after_hours_gap_pct: decimal (e.g. 0.07 = 7%). ``None`` if not
            applicable (non-earnings EP).
        premarket_volume_shares: integer. ``None`` if unknown.
        gap_confirmed_regular_session: True iff today's regular-session
            open is above prev close AND first hour sustains above open.
        neglected: from :mod:`tools.prior_rally_pct`.
        analyst_upgrades: True iff ≥1 upgrade in the relevant window.

    Returns:
        TraceEntry with ``magna_score`` (0-5), ``breakdown`` (per-letter
        bool + evidence), ``golden_ep_eligible`` (score == 5).
    """
    m_pass = (
        (eps_yoy_growth is not None and eps_yoy_growth >= MASSIVE_GROWTH_THRESHOLD)
        or (sales_yoy_growth is not None and sales_yoy_growth >= MASSIVE_GROWTH_THRESHOLD)
    )
    a_after_hours_pass = (
        after_hours_gap_pct is not None
        and premarket_volume_shares is not None
        and after_hours_gap_pct >= AFTER_HOURS_GAP_THRESHOLD
        and premarket_volume_shares >= AFTER_HOURS_VOL_THRESHOLD
    )
    g_pass = bool(gap_confirmed_regular_session)
    n_pass = bool(neglected)
    a_analyst_pass = bool(analyst_upgrades)

    breakdown = {
        "M_massive_earnings": {
            "pass": m_pass,
            "evidence": (
                f"eps_yoy={eps_yoy_growth}, sales_yoy={sales_yoy_growth}, "
                f"threshold={MASSIVE_GROWTH_THRESHOLD}"
            ),
        },
        "A_after_hours_gap": {
            "pass": a_after_hours_pass,
            "evidence": (
                f"gap={after_hours_gap_pct}, premarket_vol={premarket_volume_shares}, "
                f"thresholds={AFTER_HOURS_GAP_THRESHOLD}/{AFTER_HOURS_VOL_THRESHOLD}"
            ),
        },
        "G_gap_confirmed": {"pass": g_pass, "evidence": str(gap_confirmed_regular_session)},
        "N_neglected": {"pass": n_pass, "evidence": str(neglected)},
        "A_analyst_upgrades": {"pass": a_analyst_pass, "evidence": str(analyst_upgrades)},
    }
    score = sum(1 for v in breakdown.values() if v["pass"])

    return TraceEntry(
        tool=TOOL,
        inputs={
            "eps_yoy_growth": eps_yoy_growth,
            "sales_yoy_growth": sales_yoy_growth,
            "after_hours_gap_pct": after_hours_gap_pct,
            "premarket_volume_shares": premarket_volume_shares,
            "gap_confirmed_regular_session": gap_confirmed_regular_session,
            "neglected": neglected,
            "analyst_upgrades": analyst_upgrades,
        },
        output={
            "magna_score": score,
            "breakdown": breakdown,
            "golden_ep_eligible": score == 5,
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.magna_score",
        description="Compute MAGNA score (0-5) for EP candidate.",
    )
    p.add_argument("--eps-yoy", type=float, default=None)
    p.add_argument("--sales-yoy", type=float, default=None)
    p.add_argument("--after-hours-gap-pct", type=float, default=None)
    p.add_argument("--premarket-vol", type=int, default=None)
    p.add_argument("--gap-confirmed", action="store_true")
    p.add_argument("--neglected", action="store_true")
    p.add_argument("--analyst-upgrades", action="store_true")
    args = p.parse_args()
    emit(
        compute(
            eps_yoy_growth=args.eps_yoy,
            sales_yoy_growth=args.sales_yoy,
            after_hours_gap_pct=args.after_hours_gap_pct,
            premarket_volume_shares=args.premarket_vol,
            gap_confirmed_regular_session=args.gap_confirmed,
            neglected=args.neglected,
            analyst_upgrades=args.analyst_upgrades,
        )
    )


if __name__ == "__main__":
    main()
