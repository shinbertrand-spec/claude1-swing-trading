"""Tests for tools.position_sizer."""
from __future__ import annotations

import math

import pytest

from tools.position_sizer import compute


def test_apple_a_plus_example_concentration_binds():
    """Per swing-position-sizing.md worked example: AAPL A+ at $150k account.

    Risk budget = 2% × $150k = $3,000.
    ATR×2 = $9.14; shares-by-risk = 3000 / 9.14 ≈ 328.
    Concentration cap 25% = $37,500 / $192.74 ≈ 194 shares — binds.
    """
    e = compute(
        account=150_000.0,
        entry_price=192.74,
        atr=4.57,
        setup_grade="A+",
        regime_class="stage_2_confirmed",
    )
    assert e.output["binding_constraint"] == "concentration_cap"
    assert e.output["shares"] == 194
    assert math.isclose(e.output["capital"], 194 * 192.74, rel_tol=1e-9)
    # Effective risk should be well under 2%: 194 × 9.14 / 150000 ≈ 1.18%
    assert e.output["effective_risk_pct"] < 0.015


def test_tesla_c_grade_high_vol_minervini_cap_binds():
    """Per swing-position-sizing.md TSLA C-grade example.

    ATR=$12, ATR×2=$24 > 8% cap (=$20). Cap binds; sizer should pass back
    'minervini_8pct_cap' as the binding_constraint and emit the skip flag.
    """
    e = compute(
        account=150_000.0,
        entry_price=250.0,
        atr=12.0,
        setup_grade="C",
        regime_class="stage_2_confirmed",
    )
    assert e.output["binding_constraint"] == "minervini_8pct_cap"
    # Risk budget 0.5% × $150k = $750
    # shares-by-risk = 750 / 20 = 37.5 → 37
    # concentration: 37500 / 250 = 150
    assert e.output["shares"] == 37
    assert e.output["stop_sizer_output"]["skip_signal_atr_exceeds_cap"] is True


def test_regime_stage_4_returns_zero():
    e = compute(
        account=100_000.0,
        entry_price=100.0,
        atr=2.0,
        setup_grade="A+",
        regime_class="stage_4",
    )
    assert e.output["shares"] == 0
    assert e.output["capital"] == 0.0
    assert e.output["binding_constraint"] == "regime_stage_4_no_new_positions"
    assert e.output["regime_multiplier"] == 0.0


def test_regime_weakening_scales_risk():
    """Stage 2 weakening = 0.75× multiplier."""
    e = compute(
        account=100_000.0,
        entry_price=50.0,
        atr=1.0,
        setup_grade="A",       # 1.5%
        regime_class="stage_2_weakening",  # 0.75×
    )
    # Effective risk pct should be ~ 1.5% × 0.75 = 1.125%
    # Risk budget = 100000 × 0.01125 = 1125
    # Stop = ATR×2 = 2; shares = 1125 / 2 = 562
    # Concentration cap: 25000 / 50 = 500 — binds
    assert e.output["binding_constraint"] == "concentration_cap"
    assert e.output["shares"] == 500
    assert math.isclose(e.output["regime_multiplier"], 0.75, rel_tol=1e-9)
    assert math.isclose(e.output["base_risk_budget_pct"], 0.015, rel_tol=1e-9)


def test_unknown_setup_grade_rejected():
    with pytest.raises(ValueError, match="setup_grade"):
        compute(
            account=100_000.0,
            entry_price=100.0,
            atr=2.0,
            setup_grade="WAT",
            regime_class="stage_2_confirmed",
        )


def test_unknown_regime_rejected():
    with pytest.raises(ValueError, match="regime_class"):
        compute(
            account=100_000.0,
            entry_price=100.0,
            atr=2.0,
            setup_grade="A",
            regime_class="not_a_stage",
        )


def test_cash_available_caps_shares():
    """If cash available < computed shares × price, cash binds."""
    e = compute(
        account=150_000.0,
        entry_price=200.0,
        atr=4.0,
        setup_grade="A+",
        regime_class="stage_2_confirmed",
        cash_available=10_000.0,
    )
    # Without cash cap: concentration cap would give 187 shares.
    # With cash cap: floor(10000/200) = 50 shares.
    assert e.output["shares"] == 50
    assert e.output["binding_constraint"] == "cash_available"


def test_ep_grade_uses_2_percent_budget():
    e = compute(
        account=100_000.0,
        entry_price=100.0,
        atr=1.0,
        setup_grade="GoldenEP",
        regime_class="stage_2_confirmed",
    )
    assert math.isclose(e.output["base_risk_budget_pct"], 0.020, rel_tol=1e-9)
