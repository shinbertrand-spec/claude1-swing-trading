"""Tests for tools.thematic_portfolio.kill_switch.ladder — pure CPPI ladder.

Covers the 4 tier transitions + recovery + edge cases per the design memo
[[swing-thematic-portfolio-kill-switch-architecture]] Q10 pseudocode.

No I/O. No network. Pure logic only.
"""
from __future__ import annotations

import pytest

from tools.thematic_portfolio.kill_switch.ladder import (
    KillSwitchInputs,
    TIER_1_TARGET_ALLOCATION,
    TIER_2_TARGET_ALLOCATION,
    TIER_3_TARGET_ALLOCATION,
    compute,
    compute_as_trace,
)


# --- baseline: no trigger ---------------------------------------------------


def test_no_drawdown_holds():
    out = compute(KillSwitchInputs(
        thematic_market_value=250_000.0,
        peak_thematic_value=250_000.0,
        total_account_value=1_000_000.0,
    ))
    assert out.action == "hold"
    assert out.tier == 0
    assert out.drawdown_pct == 0.0
    assert out.current_allocation_pct == pytest.approx(0.25)
    assert out.target_allocation_pct == pytest.approx(0.25)
    assert out.sell_fraction == 0.0
    assert out.aschenbrenner_override is False


def test_small_drawdown_below_tier1_holds():
    # Peak 250k, current 210k = 16% DD, below 20% threshold
    out = compute(KillSwitchInputs(
        thematic_market_value=210_000.0,
        peak_thematic_value=250_000.0,
        total_account_value=1_000_000.0,
    ))
    assert out.action == "hold"
    assert out.tier == 0
    assert out.drawdown_pct == pytest.approx(0.16, abs=1e-6)


# --- tier 1: -20% ----------------------------------------------------------


def test_tier1_fires_at_20pct_dd_with_25pct_allocation():
    # Peak 250k, current 200k = 20% DD. Allocation = 200k/1M = 20% > 17.5%.
    out = compute(KillSwitchInputs(
        thematic_market_value=200_000.0,
        peak_thematic_value=250_000.0,
        total_account_value=1_000_000.0,
    ))
    assert out.action == "deleverage"
    assert out.tier == 1
    assert out.drawdown_pct == pytest.approx(0.20)
    assert out.target_allocation_pct == pytest.approx(0.175)
    # sell_fraction = 1 - 0.175/0.20 = 0.125
    assert out.sell_fraction == pytest.approx(0.125, abs=1e-6)


def test_tier1_already_below_target_holds():
    # Peak 250k, current 150k = 40% DD. But allocation = 150k/1M = 15% <= 17.5%.
    # Tier 1 satisfied, tier 2 fires instead (drawdown >= 35%).
    out = compute(KillSwitchInputs(
        thematic_market_value=150_000.0,
        peak_thematic_value=250_000.0,
        total_account_value=1_000_000.0,
    ))
    # Tier 2 fires: 15% > 12.5%
    assert out.tier == 2


def test_tier1_satisfied_by_prior_deleveraging():
    # We had peak 250k, sold down to 150k (manual or prior cycle).
    # Allocation now = 15% / 1M = 15% which is below 17.5% but above 12.5%.
    # Drawdown = 100k / 250k = 40% — tier 2 territory.
    out = compute(KillSwitchInputs(
        thematic_market_value=150_000.0,
        peak_thematic_value=250_000.0,
        total_account_value=1_000_000.0,
        previous_fired_tier=1,
    ))
    # Tier 2 still fires by drawdown
    assert out.tier == 2

    # Now bring allocation to 12.5% exactly — tier 2 should be satisfied
    out2 = compute(KillSwitchInputs(
        thematic_market_value=125_000.0,
        peak_thematic_value=250_000.0,
        total_account_value=1_000_000.0,
        previous_fired_tier=2,
    ))
    # DD = 50%, but tier 3 fires
    assert out2.tier == 3

    # Bring allocation to 12.5% with smaller drawdown (-40% < 50%) → tier 2 satisfied
    # Peak 100k, current 60k = 40% DD; allocation 60k/1M = 6% (well below 12.5%)
    out3 = compute(KillSwitchInputs(
        thematic_market_value=60_000.0,
        peak_thematic_value=100_000.0,
        total_account_value=1_000_000.0,
    ))
    # DD 40% >= 35% but allocation 6% < 12.5% → tier 2 hold
    assert out3.action == "hold"
    assert out3.tier == 0
    assert "Tier 2 satisfied" in out3.rationale


# --- tier 2: -35% ----------------------------------------------------------


def test_tier2_fires_at_35pct_dd():
    # Peak 250k, current 162.5k = 35% DD. Allocation 162.5k/1M = 16.25%.
    # 16.25% > 12.5%, so tier 2 fires.
    out = compute(KillSwitchInputs(
        thematic_market_value=162_500.0,
        peak_thematic_value=250_000.0,
        total_account_value=1_000_000.0,
    ))
    assert out.action == "deleverage"
    assert out.tier == 2
    assert out.drawdown_pct == pytest.approx(0.35)
    assert out.target_allocation_pct == pytest.approx(0.125)
    # sell_fraction = 1 - 0.125/0.1625 ≈ 0.2308
    assert out.sell_fraction == pytest.approx(1.0 - 0.125 / 0.1625, abs=1e-6)


# --- tier 3: -50% or aschenbrenner kill-event ------------------------------


def test_tier3_fires_at_50pct_dd():
    # Peak 250k, current 125k = 50% DD. Allocation 12.5%.
    out = compute(KillSwitchInputs(
        thematic_market_value=125_000.0,
        peak_thematic_value=250_000.0,
        total_account_value=1_000_000.0,
    ))
    assert out.action == "unwind"
    assert out.tier == 3
    assert out.drawdown_pct == pytest.approx(0.50)
    assert out.target_allocation_pct == 0.0
    assert out.sell_fraction == 1.0


def test_aschenbrenner_kill_event_fires_tier3_regardless_of_dd():
    out = compute(KillSwitchInputs(
        thematic_market_value=250_000.0,  # at peak, no drawdown
        peak_thematic_value=250_000.0,
        total_account_value=1_000_000.0,
        aschenbrenner_kill_event=True,
    ))
    assert out.action == "unwind"
    assert out.tier == 3
    assert out.aschenbrenner_override is True
    assert out.sell_fraction == 1.0
    assert "aschenbrenner_kill_event=True" in out.rationale


def test_aschenbrenner_kill_event_with_zero_allocation_holds():
    # Flag set but we have no thematic exposure -> nothing to sell.
    out = compute(KillSwitchInputs(
        thematic_market_value=0.0,
        peak_thematic_value=0.0,
        total_account_value=1_000_000.0,
        aschenbrenner_kill_event=True,
    ))
    assert out.action == "hold"
    assert out.tier == 0
    assert "already fully unwound" in out.rationale


# --- edge cases ------------------------------------------------------------


def test_peak_zero_no_drawdown():
    out = compute(KillSwitchInputs(
        thematic_market_value=0.0,
        peak_thematic_value=0.0,
        total_account_value=1_000_000.0,
    ))
    assert out.action == "hold"
    assert out.tier == 0
    assert out.drawdown_pct == 0.0


def test_total_account_value_zero_caps_allocation_at_zero():
    out = compute(KillSwitchInputs(
        thematic_market_value=100_000.0,
        peak_thematic_value=100_000.0,
        total_account_value=0.0,
    ))
    assert out.action == "hold"
    assert out.current_allocation_pct == 0.0


def test_peak_below_current_warns_and_uses_current_as_effective_peak():
    out = compute(KillSwitchInputs(
        thematic_market_value=300_000.0,
        peak_thematic_value=250_000.0,  # caller forgot to update
        total_account_value=1_000_000.0,
    ))
    assert out.drawdown_pct == 0.0
    assert any("peak_thematic_value" in w for w in out.warnings)


def test_compute_as_trace_returns_trace_entry():
    entry = compute_as_trace(KillSwitchInputs(
        thematic_market_value=200_000.0,
        peak_thematic_value=250_000.0,
        total_account_value=1_000_000.0,
    ))
    assert entry.tool == "tools/thematic_portfolio/kill_switch/ladder.py"
    assert entry.inputs["thematic_market_value"] == 200_000.0
    assert entry.output["tier"] == 1
    assert entry.fetched_at  # ISO timestamp


# --- sell_fraction math ----------------------------------------------------


@pytest.mark.parametrize("alloc_pct,target_pct,expected", [
    (0.25, 0.175, 1.0 - 0.175/0.25),     # 30%
    (0.20, 0.175, 1.0 - 0.175/0.20),     # 12.5%
    (0.175, 0.175, 0.0),                 # exactly at target
    (0.10, 0.125, 0.0),                  # already below target
    (0.25, 0.0, 1.0),                    # tier 3 full unwind
])
def test_sell_fraction_math_direct(alloc_pct, target_pct, expected):
    # Synthesize inputs that produce the alloc + matching tier.
    # Use peak 1.0 so drawdown = 1 - market_value/1.0
    # For tier 1: alloc>17.5% AND DD>=20% — use DD=22%
    # For tier 3: aschenbrenner=True
    if target_pct == 0.0:
        out = compute(KillSwitchInputs(
            thematic_market_value=alloc_pct * 1_000_000.0,
            peak_thematic_value=alloc_pct * 1_000_000.0,
            total_account_value=1_000_000.0,
            aschenbrenner_kill_event=True,
        ))
    else:
        # Force a drawdown high enough for tier 1+. Set peak = market_value/0.7
        # -> DD = 30%. (alloc_pct is computed against total_account, so peak
        # only affects drawdown.)
        market = alloc_pct * 1_000_000.0
        out = compute(KillSwitchInputs(
            thematic_market_value=market,
            peak_thematic_value=market / 0.7,
            total_account_value=1_000_000.0,
        ))
    assert out.sell_fraction == pytest.approx(expected, abs=1e-6)
