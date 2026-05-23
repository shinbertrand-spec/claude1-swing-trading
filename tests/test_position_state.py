"""Tests for tools.position_state."""
from __future__ import annotations

import math

import pytest

from tools.position_state import compute


def test_starter_only():
    e = compute(starter_shares=20, starter_price=415.80, current_price=420.0)
    assert e.output["stage"] == "STARTER"
    assert e.output["total_shares"] == 20
    assert math.isclose(e.output["combined_breakeven"], 415.80, rel_tol=1e-9)
    assert math.isclose(e.output["unrealized_pnl_pct"], (420.0 / 415.80) - 1.0, rel_tol=1e-9)


def test_stage_2_after_addon_1():
    e = compute(
        starter_shares=20, starter_price=415.80,
        addon1_shares=40, addon1_price=448.20,
        current_price=460.0,
    )
    assert e.output["stage"] == "Stage-2"
    assert e.output["total_shares"] == 60
    expected_be = (20 * 415.80 + 40 * 448.20) / 60
    assert math.isclose(e.output["combined_breakeven"], expected_be, rel_tol=1e-9)


def test_stage_3_full_pyramid():
    e = compute(
        starter_shares=20, starter_price=415.80,
        addon1_shares=40, addon1_price=448.20,
        addon2_shares=30, addon2_price=465.40,
        current_price=478.20,
    )
    assert e.output["stage"] == "Stage-3"
    assert e.output["total_shares"] == 90


def test_addon2_without_addon1_invalid():
    e = compute(
        starter_shares=20, starter_price=415.80,
        addon2_shares=30, addon2_price=465.40,
    )
    assert e.output["stage"] == "invalid"
    assert "addon_2" in e.output["error"]


def test_closed_position():
    e = compute(starter_shares=None, starter_price=None, closed=True)
    assert e.output["stage"] == "closed"


def test_open_without_starter_raises():
    with pytest.raises(ValueError, match="starter leg"):
        compute(starter_shares=None, starter_price=None)


def test_unrealized_pnl_negative():
    e = compute(starter_shares=20, starter_price=415.80, current_price=400.0)
    assert e.output["unrealized_pnl_pct"] < 0
    assert e.output["unrealized_pnl_usd"] < 0
