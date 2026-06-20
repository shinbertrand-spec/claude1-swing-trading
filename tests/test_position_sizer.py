"""Tests for tools.position_sizer."""
from __future__ import annotations

import math

import pytest

from tools.position_sizer import compute


def test_apple_a_plus_example_concentration_binds():
    """Per swing-position-sizing.md worked example: AAPL A+ at $150k account.

    Risk budget = 2% × $150k = $3,000.
    ATR×2 = $9.14; shares-by-risk = 3000 / 9.14 ≈ 328.
    Concentration cap 10% (reconciled 2026-06-20 from 25%) =
    $15,000 / $192.74 ≈ 77 shares — binds.
    """
    e = compute(
        account=150_000.0,
        entry_price=192.74,
        atr=4.57,
        setup_grade="A+",
        regime_class="stage_2_confirmed",
    )
    assert e.output["binding_constraint"] == "concentration_cap"
    assert e.output["shares"] == 77
    assert math.isclose(e.output["capital"], 77 * 192.74, rel_tol=1e-9)
    # Position capital must not exceed the 10% per-position cap.
    assert e.output["capital_pct"] <= 0.10
    # Effective risk should be well under 2%: 77 × 9.14 / 150000 ≈ 0.47%
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
    # concentration (10% cap): 15000 / 250 = 60 — does not bind (risk does)
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
    # Concentration cap 10% (reconciled 2026-06-20): 10000 / 50 = 200 — binds
    assert e.output["binding_constraint"] == "concentration_cap"
    assert e.output["shares"] == 200
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
    # Without cash cap: 10% concentration cap gives 75 shares (15000/200).
    # With cash cap: floor(10000/200) = 50 shares — cash binds.
    assert e.output["shares"] == 50
    assert e.output["binding_constraint"] == "cash_available"


def test_default_cap_is_ten_percent_and_clamps():
    """Reconciliation 2026-06-20: the default per-position cap is 10%, and a
    position whose risk-budget size would exceed 10% of the account is clamped
    to 10% with binding_constraint == concentration_cap.
    """
    from tools.position_sizer import DEFAULT_CONCENTRATION_CAP_PCT

    assert DEFAULT_CONCENTRATION_CAP_PCT == 0.10
    # Tight stop (low ATR) -> risk-budget path would buy a huge position;
    # the 10% cap must clamp it.
    e = compute(
        account=100_000.0,
        entry_price=100.0,
        atr=0.50,            # ATR×2 = $1 stop -> shares_by_risk = 2000/1 = 2000
        setup_grade="A+",    # 2% budget = $2,000
        regime_class="stage_2_confirmed",
    )
    assert e.output["binding_constraint"] == "concentration_cap"
    # 10% of 100k / $100 = 100 shares exactly.
    assert e.output["shares"] == 100
    assert math.isclose(e.output["capital_pct"], 0.10, rel_tol=1e-9)


def test_explicit_tighter_cap_still_honoured():
    """The paper-auto/quant callers pin a tighter 0.05 explicitly; the sizer
    must still honour an explicit override below the new 10% default.
    """
    e = compute(
        account=100_000.0,
        entry_price=100.0,
        atr=0.50,
        setup_grade="A+",
        regime_class="stage_2_confirmed",
        concentration_cap_pct=0.05,
    )
    assert e.output["binding_constraint"] == "concentration_cap"
    assert e.output["shares"] == 50   # 5% of 100k / $100
    assert math.isclose(e.output["capital_pct"], 0.05, rel_tol=1e-9)


def test_ep_grade_uses_2_percent_budget():
    e = compute(
        account=100_000.0,
        entry_price=100.0,
        atr=1.0,
        setup_grade="GoldenEP",
        regime_class="stage_2_confirmed",
    )
    assert math.isclose(e.output["base_risk_budget_pct"], 0.020, rel_tol=1e-9)
