"""EP grade — Super Swan / Swan / Duck / Chicken / 🏆 Golden EP.

Per ``swing-earnings-pivot.md`` 4-tier framework (Stockbee). Decision rules
combine MAGNA score (from :mod:`tools.magna_score`), gap percent (from
:mod:`tools.ep_detect`), intraday expansion, and earnings-beat status.

Grading (from the operational note):

* **Golden EP** 🏆 — Swan/Super Swan + gap in 10-19% sweet spot + intraday
  expansion ≥5%. 92.3% sustain rate beyond 20 days per Stockbee.
* **Super Swan** — ≥10% intraday expansion + neglected + earnings beat.
  4.2% Day 1 failure, 61.5% sustain 20+ days.
* **Swan** — high-quality EP, MAGNA ≥4. High sustain.
* **Duck** — moderate quality, MAGNA 2-3 or marginal signals.
* **Chicken** — marginal, MAGNA ≤1 or missing key signals.

Gap risk bands (per the note's risk-band table) modify the grade:
* 5-9% gap: best odds
* 10-19% gap: sweet spot for Golden EP
* 20%+ gap: 44.8% Day 1 failure rate — downgrade

Pure compute — no data fetch.

CLI::

    uv run python -m tools.ep_grade --magna 5 --gap-pct 0.142 \\
        --intraday-expansion-pct 0.062 --earnings-beat --neglected
"""
from __future__ import annotations

import argparse

from .cli import emit
from .contract import TraceEntry

TOOL = "tools/ep_grade.py"

GAP_BAND_SMALL = (0.05, 0.10)       # 5-9% — best odds
GAP_BAND_SWEET = (0.10, 0.20)       # 10-19% — Golden EP sweet spot
GAP_BAND_LARGE = (0.20, float("inf"))  # 20%+ — high failure risk

SUPER_SWAN_INTRADAY_EXPANSION = 0.10  # ≥10%
GOLDEN_INTRADAY_EXPANSION = 0.05      # ≥5%


def _gap_band(gap_pct: float) -> str:
    if gap_pct >= GAP_BAND_LARGE[0]:
        return "large_20_plus"
    if gap_pct >= GAP_BAND_SWEET[0]:
        return "sweet_10_to_19"
    if gap_pct >= GAP_BAND_SMALL[0]:
        return "small_5_to_9"
    return "below_threshold"


def compute(
    magna_score: int,
    gap_pct: float,
    intraday_expansion_pct: float | None,
    earnings_beat: bool,
    neglected: bool,
) -> TraceEntry:
    """Apply the grading decision matrix.

    Args:
        magna_score: 0-5 from :mod:`tools.magna_score`.
        gap_pct: open-gap as decimal (e.g. 0.142 = 14.2%).
        intraday_expansion_pct: today's % expansion above open
            (e.g. 0.062 = 6.2%). Phase 2: caller may not have intraday
            data; pass ``None`` to omit Super Swan / Golden EP upgrades.
        earnings_beat: True iff the catalyst is an earnings beat ≥10%
            consensus surprise (per ``post-earnings-drift`` threshold).
        neglected: True iff prior-rally check passed.

    Returns:
        TraceEntry with ``grade`` ∈ {GoldenEP, SuperSwan, Swan, Duck, Chicken}
        + ``gap_band`` + reasoning fields.
    """
    if not 0 <= magna_score <= 5:
        raise ValueError(f"magna_score must be 0-5; got {magna_score}")
    if gap_pct < 0:
        raise ValueError(f"gap_pct must be non-negative; got {gap_pct}")

    band = _gap_band(gap_pct)

    # Baseline grade from MAGNA + gap band.
    if magna_score == 5:
        baseline = "Swan"  # candidate for upgrade
    elif magna_score == 4:
        baseline = "Swan"
    elif magna_score in (2, 3):
        baseline = "Duck"
    else:
        baseline = "Chicken"

    grade = baseline
    rationale: list[str] = [f"baseline={baseline} from magna_score={magna_score}"]

    # Super Swan upgrade: high intraday expansion + neglected + earnings beat.
    super_swan_eligible = (
        intraday_expansion_pct is not None
        and intraday_expansion_pct >= SUPER_SWAN_INTRADAY_EXPANSION
        and neglected
        and earnings_beat
    )
    if super_swan_eligible and grade == "Swan":
        grade = "SuperSwan"
        rationale.append(
            "upgraded to SuperSwan: intraday_expansion>=10% + neglected + earnings_beat"
        )

    # Golden EP upgrade: Swan/Super Swan + sweet-spot gap + expansion >=5%.
    golden_eligible = (
        grade in {"Swan", "SuperSwan"}
        and band == "sweet_10_to_19"
        and intraday_expansion_pct is not None
        and intraday_expansion_pct >= GOLDEN_INTRADAY_EXPANSION
    )
    if golden_eligible:
        grade = "GoldenEP"
        rationale.append(
            "upgraded to GoldenEP: Swan/SuperSwan + gap in 10-19% sweet spot + expansion>=5%"
        )

    # Large-gap downgrade: 20%+ gap halves the grade tier.
    if band == "large_20_plus" and grade in {"GoldenEP", "SuperSwan", "Swan"}:
        downgraded = {"GoldenEP": "Swan", "SuperSwan": "Swan", "Swan": "Duck"}
        new_grade = downgraded[grade]
        rationale.append(
            f"downgraded {grade} -> {new_grade}: gap_band=large_20_plus (44.8% Day 1 failure)"
        )
        grade = new_grade

    return TraceEntry(
        tool=TOOL,
        inputs={
            "magna_score": magna_score,
            "gap_pct": gap_pct,
            "intraday_expansion_pct": intraday_expansion_pct,
            "earnings_beat": earnings_beat,
            "neglected": neglected,
        },
        output={
            "grade": grade,
            "gap_band": band,
            "rationale": rationale,
            "super_swan_eligible_pre_band_check": super_swan_eligible,
            "golden_eligible_pre_band_check": golden_eligible,
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.ep_grade",
        description="EP grading: Super Swan / Swan / Duck / Chicken / Golden EP.",
    )
    p.add_argument("--magna", type=int, required=True, dest="magna_score")
    p.add_argument("--gap-pct", type=float, required=True)
    p.add_argument("--intraday-expansion-pct", type=float, default=None)
    p.add_argument("--earnings-beat", action="store_true")
    p.add_argument("--neglected", action="store_true")
    args = p.parse_args()
    emit(
        compute(
            magna_score=args.magna_score,
            gap_pct=args.gap_pct,
            intraday_expansion_pct=args.intraday_expansion_pct,
            earnings_beat=args.earnings_beat,
            neglected=args.neglected,
        )
    )


if __name__ == "__main__":
    main()
