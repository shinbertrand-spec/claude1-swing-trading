"""Tests for tools.add_on_evaluator."""
from __future__ import annotations

import math

from tools.add_on_evaluator import compute


def test_addon_1_fires_at_breakout():
    """STARTER stage + Momentum Burst triggered + Stage 2 confirmed → ADD-ON #1."""
    e = compute(
        current_stage="STARTER",
        triggered=True,
        setup_grade="GoldenEP",
        regime_class="stage_2_confirmed",
        starter_shares=20,
        starter_price=415.80,
        intended_full_shares=60,
        current_price=437.00,   # under chase threshold (415.80 * 1.05 = 436.59... actually just over)
    )
    # 437 / 415.80 = 5.10% — just over chase. Use a price within tolerance.
    # Re-do with safer current_price.
    e = compute(
        current_stage="STARTER",
        triggered=True,
        setup_grade="GoldenEP",
        regime_class="stage_2_confirmed",
        starter_shares=20,
        starter_price=415.80,
        intended_full_shares=60,
        current_price=430.00,
    )
    assert e.output["action"] == "add"
    assert e.output["add_shares"] == 40   # 60 - 20
    assert e.output["new_total_shares"] == 60
    assert e.output["stage_after"] == "Stage-2"
    assert e.output["trail_ma"] == "combined_breakeven"
    expected_be = (20 * 415.80 + 40 * 430.00) / 60
    assert math.isclose(e.output["new_combined_breakeven"], expected_be, rel_tol=1e-9)


def test_addon_2_fires_for_super_swan():
    """Stage-2 + Day 7 milestone + Super Swan + Stage 2 confirmed → ADD-ON #2."""
    e = compute(
        current_stage="Stage-2",
        triggered=True,
        setup_grade="SuperSwan",
        regime_class="stage_2_confirmed",
        starter_shares=20,
        starter_price=415.80,
        intended_full_shares=60,
        current_price=465.00,
        addon1_shares=40,
        addon1_price=448.20,
    )
    assert e.output["action"] == "add"
    assert e.output["add_shares"] == 30   # 50% of 60
    assert e.output["new_total_shares"] == 90
    assert e.output["stage_after"] == "Stage-3"
    assert e.output["trail_ma"] == "10_day_MA"


def test_addon_2_blocked_for_swan_grade():
    """Swan (not Super Swan / Golden EP) → no Day 7 add per swing-momentum-execution."""
    e = compute(
        current_stage="Stage-2",
        triggered=True,
        setup_grade="Swan",
        regime_class="stage_2_confirmed",
        starter_shares=20,
        starter_price=415.80,
        intended_full_shares=60,
        current_price=465.00,
        addon1_shares=40,
        addon1_price=448.20,
    )
    assert e.output["action"] == "skip"
    assert "restricted" in e.output["reason"]


def test_addon_1_blocked_in_stage_3_regime():
    """Stage-2 weakening regime blocks ADD-ON #2 (tighter regime gate) but allows #1."""
    # ADD-ON #1 in stage_2_weakening allowed
    e = compute(
        current_stage="STARTER",
        triggered=True,
        setup_grade="GoldenEP",
        regime_class="stage_2_weakening",
        starter_shares=20,
        starter_price=415.80,
        intended_full_shares=60,
        current_price=420.0,
    )
    assert e.output["action"] == "add"

    # ADD-ON #2 in stage_2_weakening blocked
    e2 = compute(
        current_stage="Stage-2",
        triggered=True,
        setup_grade="GoldenEP",
        regime_class="stage_2_weakening",
        starter_shares=20,
        starter_price=415.80,
        intended_full_shares=60,
        current_price=465.00,
        addon1_shares=40,
        addon1_price=448.20,
    )
    assert e2.output["action"] == "skip"
    assert "regime" in e2.output["reason"]


def test_chase_detected_skips():
    """Price extended >5% above last leg → chase detected."""
    e = compute(
        current_stage="STARTER",
        triggered=True,
        setup_grade="GoldenEP",
        regime_class="stage_2_confirmed",
        starter_shares=20,
        starter_price=415.80,
        intended_full_shares=60,
        current_price=440.0,   # > 415.80 * 1.05 = 436.59
    )
    assert e.output["action"] == "skip"
    assert "chase" in e.output["reason"]


def test_no_trigger_skips():
    e = compute(
        current_stage="STARTER",
        triggered=False,
        setup_grade="GoldenEP",
        regime_class="stage_2_confirmed",
        starter_shares=20,
        starter_price=415.80,
        intended_full_shares=60,
        current_price=420.0,
    )
    assert e.output["action"] == "skip"
    assert "trigger" in e.output["reason"]


def test_no_op_from_invalid_stage():
    e = compute(
        current_stage="Stage-3",  # already pyramided fully
        triggered=True,
        setup_grade="GoldenEP",
        regime_class="stage_2_confirmed",
        starter_shares=20,
        starter_price=415.80,
        intended_full_shares=60,
        current_price=475.00,
    )
    assert e.output["action"] == "no_op"


def test_concentration_cap_blocks():
    e = compute(
        current_stage="STARTER",
        triggered=True,
        setup_grade="GoldenEP",
        regime_class="stage_2_confirmed",
        starter_shares=20,
        starter_price=415.80,
        intended_full_shares=60,
        current_price=420.0,
        concentration_cap_shares=20,   # already at cap
    )
    assert e.output["action"] == "skip"
    assert "concentration_cap" in e.output["reason"]
