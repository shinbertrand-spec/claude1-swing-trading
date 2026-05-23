"""Tests for tools.sell_into_strength."""
from __future__ import annotations

import math

import pytest

from tools.sell_into_strength import compute


def test_triggers_high_grade_50pct():
    """A+ / GoldenEP / SuperSwan default to 50% trim."""
    for grade in ("A+", "GoldenEP", "SuperSwan"):
        e = compute(gain_pct=0.12, days_in_move=2, setup_grade=grade)
        assert e.output["threshold_met"] is True
        assert math.isclose(e.output["recommended_fraction"], 0.50)


def test_triggers_low_grade_80pct():
    """Lower grades default to 80% trim."""
    for grade in ("A", "B", "C", "Swan", "Duck", "Chicken"):
        e = compute(gain_pct=0.12, days_in_move=2, setup_grade=grade)
        assert e.output["threshold_met"] is True
        assert math.isclose(e.output["recommended_fraction"], 0.80)


def test_below_gain_threshold_no_trigger():
    e = compute(gain_pct=0.05, days_in_move=2, setup_grade="GoldenEP")
    assert e.output["threshold_met"] is False
    assert e.output["recommended_fraction"] == 0.0


def test_too_many_days_no_trigger():
    e = compute(gain_pct=0.15, days_in_move=10, setup_grade="GoldenEP")
    assert e.output["threshold_met"] is False


def test_catalyst_pending_suppresses_trigger():
    e = compute(gain_pct=0.12, days_in_move=2, setup_grade="GoldenEP", catalyst_pending=True)
    assert e.output["threshold_met"] is False
    assert "catalyst_pending" in " ".join(e.output["rationale"])


def test_invalid_days_rejected():
    with pytest.raises(ValueError, match="days_in_move"):
        compute(gain_pct=0.12, days_in_move=0, setup_grade="GoldenEP")
