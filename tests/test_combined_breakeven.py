"""Tests for tools.combined_breakeven."""
from __future__ import annotations

import math

import pytest

from tools.combined_breakeven import compute


def test_single_leg():
    e = compute([(100, 50.0)])
    assert e.output["combined_breakeven"] == 50.0
    assert e.output["total_shares"] == 100
    assert e.output["leg_count"] == 1


def test_two_legs_weighted_average():
    """SMCI pyramid: STARTER 20 @ $415.80, ADD-ON #1 40 @ $448.20.
    Weighted: (20*415.80 + 40*448.20) / 60 = $437.40."""
    e = compute([(20, 415.80), (40, 448.20)])
    assert math.isclose(e.output["combined_breakeven"], 437.40, rel_tol=1e-9)
    assert e.output["total_shares"] == 60


def test_three_legs_full_pyramid():
    """Full SMCI pyramid through ADD-ON #2: 20+40+30 @ $415.80/$448.20/$465.40."""
    e = compute([(20, 415.80), (40, 448.20), (30, 465.40)])
    expected = (20 * 415.80 + 40 * 448.20 + 30 * 465.40) / 90
    assert math.isclose(e.output["combined_breakeven"], expected, rel_tol=1e-9)
    assert e.output["total_shares"] == 90


def test_empty_legs_raises():
    with pytest.raises(ValueError, match="non-empty"):
        compute([])


def test_nonpositive_shares_raises():
    with pytest.raises(ValueError, match="shares"):
        compute([(0, 100.0)])


def test_nonpositive_price_raises():
    with pytest.raises(ValueError, match="price"):
        compute([(10, -5.0)])
