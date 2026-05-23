"""Tests for tools.ep_grade."""
from __future__ import annotations

import pytest

from tools.ep_grade import compute


def test_golden_ep_smci_like():
    """5 MAGNA + 14.2% gap (sweet spot) + 6.2% intraday + earnings beat +
    neglected → GoldenEP."""
    e = compute(
        magna_score=5,
        gap_pct=0.142,
        intraday_expansion_pct=0.062,
        earnings_beat=True,
        neglected=True,
    )
    assert e.output["grade"] == "GoldenEP"
    assert e.output["gap_band"] == "sweet_10_to_19"


def test_super_swan_without_sweet_spot():
    """5 MAGNA + small gap (7%) + 12% intraday + beat + neglected → SuperSwan
    (no Golden upgrade because gap not in sweet spot)."""
    e = compute(
        magna_score=5,
        gap_pct=0.07,
        intraday_expansion_pct=0.12,
        earnings_beat=True,
        neglected=True,
    )
    assert e.output["grade"] == "SuperSwan"
    assert e.output["gap_band"] == "small_5_to_9"


def test_large_gap_downgrades_golden_to_swan():
    """GoldenEP-candidate but gap 25% → downgrade to Swan."""
    e = compute(
        magna_score=5,
        gap_pct=0.25,
        intraday_expansion_pct=0.08,
        earnings_beat=True,
        neglected=True,
    )
    # Pre-band check would have made it Golden (large gap is NOT sweet so
    # actually Golden upgrade fails) — but Super Swan upgrade can still
    # fire, then the large-band downgrade brings it down.
    assert e.output["gap_band"] == "large_20_plus"
    assert e.output["grade"] in {"Swan", "Duck"}


def test_swan_with_4_magna():
    e = compute(
        magna_score=4,
        gap_pct=0.12,
        intraday_expansion_pct=0.03,  # below 5% threshold for Golden
        earnings_beat=False,
        neglected=False,
    )
    # baseline Swan; no Super Swan upgrade (no beat/neglected); no Golden upgrade
    # (expansion < 5%); no large-gap downgrade.
    assert e.output["grade"] == "Swan"


def test_duck_for_magna_2_or_3():
    for s in (2, 3):
        e = compute(
            magna_score=s,
            gap_pct=0.12,
            intraday_expansion_pct=None,
            earnings_beat=False,
            neglected=False,
        )
        assert e.output["grade"] == "Duck"


def test_chicken_for_magna_0_or_1():
    for s in (0, 1):
        e = compute(
            magna_score=s,
            gap_pct=0.12,
            intraday_expansion_pct=None,
            earnings_beat=False,
            neglected=False,
        )
        assert e.output["grade"] == "Chicken"


def test_golden_requires_intraday_data():
    """Without intraday_expansion_pct, GoldenEP upgrade can't fire."""
    e = compute(
        magna_score=5,
        gap_pct=0.142,
        intraday_expansion_pct=None,
        earnings_beat=True,
        neglected=True,
    )
    assert e.output["grade"] != "GoldenEP"
    assert e.output["grade"] in {"Swan", "SuperSwan"}


def test_rejects_invalid_magna():
    with pytest.raises(ValueError, match="magna_score"):
        compute(
            magna_score=6,
            gap_pct=0.1,
            intraday_expansion_pct=None,
            earnings_beat=False,
            neglected=False,
        )


def test_rejects_negative_gap():
    with pytest.raises(ValueError, match="gap_pct"):
        compute(
            magna_score=3,
            gap_pct=-0.01,
            intraday_expansion_pct=None,
            earnings_beat=False,
            neglected=False,
        )
