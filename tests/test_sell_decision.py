"""Tests for tools.sell_decision."""
from __future__ import annotations

import pytest

from tools.sell_decision import compute


def _kwargs(**overrides):
    base = dict(
        climax_patterns_firing=0,
        violations_firing=0,
        violation_5_alone_full_exit=False,
        base_stage=1,
        new_high_today=False,
        sell_into_strength_triggered=False,
        sell_into_strength_fraction=0.0,
        setup_grade="GoldenEP",
        pe_expansion_warning=False,
        regime_class="stage_2_confirmed",
    )
    base.update(overrides)
    return base


def test_no_signals_returns_hold():
    e = compute(**_kwargs())
    assert e.output["action"] == "hold"
    assert e.output["contributing_triggers"] == []
    assert e.output["v1_preliminary_flag"] is True


def test_stage_4_regime_forces_exit_all():
    e = compute(**_kwargs(regime_class="stage_4"))
    assert e.output["action"] == "sell_100"
    assert "regime_stage_4" in e.output["contributing_triggers"][0]


def test_violation_5_alone_full_exit():
    e = compute(**_kwargs(violations_firing=1, violation_5_alone_full_exit=True))
    assert e.output["action"] == "sell_100"


def test_three_climax_patterns_sell_75():
    e = compute(**_kwargs(climax_patterns_firing=3))
    assert e.output["action"] == "sell_75"


def test_two_climax_patterns_high_grade_no_action():
    """GoldenEP requires 3+ climax patterns per the modifier table — 2 is below threshold."""
    e = compute(**_kwargs(climax_patterns_firing=2))
    assert e.output["action"] == "hold"


def test_two_climax_patterns_swan_grade_sell_50():
    """Swan threshold is 2 → 2 patterns fire sell_50."""
    e = compute(**_kwargs(climax_patterns_firing=2, setup_grade="Swan"))
    assert e.output["action"] == "sell_50"


def test_one_climax_chicken_grade_sell_50():
    """Chicken threshold is 1 → 1 pattern fires sell_50."""
    e = compute(**_kwargs(climax_patterns_firing=1, setup_grade="Chicken"))
    assert e.output["action"] == "sell_50"


def test_three_violations_full_exit():
    e = compute(**_kwargs(violations_firing=3, setup_grade="Swan"))
    assert e.output["action"] == "sell_100"


def test_one_violation_partial_action():
    """Single violation → tighten + 1/3 partial (max action = sell_1_3)."""
    e = compute(**_kwargs(violations_firing=1, setup_grade="GoldenEP"))
    assert e.output["action"] == "sell_1_3"


def test_late_stage_new_high_triggers_sell_75():
    e = compute(**_kwargs(base_stage=4, new_high_today=True))
    assert e.output["action"] == "sell_75"


def test_third_stage_new_high_partial():
    e = compute(**_kwargs(base_stage=3, new_high_today=True))
    assert e.output["action"] == "sell_1_3"


def test_sell_into_strength_50():
    e = compute(**_kwargs(
        sell_into_strength_triggered=True,
        sell_into_strength_fraction=0.50,
    ))
    assert e.output["action"] == "sell_50"


def test_sell_into_strength_80_maps_to_sell_75():
    """80% fraction maps to sell_75 (closest discrete action)."""
    e = compute(**_kwargs(
        sell_into_strength_triggered=True,
        sell_into_strength_fraction=0.80,
        setup_grade="Chicken",
    ))
    assert e.output["action"] == "sell_75"


def test_unknown_grade_rejected():
    with pytest.raises(ValueError, match="setup_grade"):
        compute(**_kwargs(setup_grade="WAT"))


def test_pe_expansion_alone_tightens():
    e = compute(**_kwargs(pe_expansion_warning=True))
    assert e.output["action"] == "tighten_stop"
    assert "pe_expansion" in " ".join(e.output["contributing_triggers"])
