"""Tests for tools.thematic_portfolio.put_overlay."""
from __future__ import annotations

import math

import pytest

from tools.thematic_portfolio import Position
from tools.thematic_portfolio.put_overlay import (
    MAX_CASH_RAISE_PCT_OF_LONG_BOOK,
    MAX_NVDA_PUTS_PCT_OF_TOTAL,
    NVDA_PUTS_DEFAULT_PCT_OF_TOTAL,
    compute,
)


# Q1 2026 SA LP slice (rough — values in millions, scaled to test the math
# without needing the full 42-row infotable in test fixtures):
SA_LP_LONG_BOOK = [
    Position("BE", "BLOOM ENERGY", 877.0e6),
    Position("SNDK", "SANDISK", 724.0e6),
    Position("CRWV", "COREWEAVE", 556.0e6),
    Position("IREN", "IREN", 401.0e6),
    Position("CORZ", "CORE SCIENTIFIC", 389.0e6),
    Position("APLD", "APPLIED DIGITAL", 320.0e6),
    Position("RIOT", "RIOT PLATFORMS", 142.0e6),
    Position("CLSK", "CLEANSPARK", 104.0e6),
    # Tail of 18 smaller positions
    *[Position(f"TAIL{i}", f"TAIL{i}", 20.0e6) for i in range(18)],
]
SA_LP_LONG_TOTAL = sum(p.value_usd for p in SA_LP_LONG_BOOK)  # ~3.87B

# Q1 2026 SA LP put complex slice. Notional values per davemanuel synthesis.
SA_LP_PUT_COMPLEX = [
    Position("SMH", "VANECK SEMICONDUCTOR ETF", 2.04e9),
    Position("NVDA", "NVIDIA CORP", 1.57e9),
    Position("ORCL", "ORACLE CORP", 1.07e9),
    Position("AVGO", "BROADCOM INC", 1.01e9),
    Position("AMD", "ADVANCED MICRO DEVICES", 969.0e6),
    Position("MU", "MICRON TECH", 584.0e6),
    Position("TSM", "TAIWAN SEMI", 535.0e6),
    Position("ASML", "ASML HOLDING", 494.0e6),
    Position("INTC", "INTEL CORP", 159.0e6),
]
SA_LP_PUT_TOTAL = sum(p.value_usd for p in SA_LP_PUT_COMPLEX)  # ~8.46B

# Light Street Q1 2026 ~$0.50B book. Roughly half chip-bullish.
LIGHT_STREET_LONG_BOOK = [
    Position("NVDA", "NVIDIA CORP", 100.0e6),
    Position("AVGO", "BROADCOM INC", 80.0e6),
    Position("MU", "MICRON TECH", 60.0e6),
    Position("BE", "BLOOM ENERGY", 50.0e6),       # non-chip
    Position("CRWV", "COREWEAVE", 40.0e6),         # non-chip (data center)
    Position("ABCD", "OTHER", 30.0e6),
    Position("EFGH", "OTHER", 20.0e6),
]


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------


def test_compute_q1_2026_worked_example_caps_cash_raise():
    """Q1 2026 barbell is ~2.19× (8.46/3.87). Cap should bind at 50%."""
    entry = compute(
        sa_lp_long_book=SA_LP_LONG_BOOK,
        sa_lp_put_complex=SA_LP_PUT_COMPLEX,
        light_street_long_book=LIGHT_STREET_LONG_BOOK,
        thematic_allocation_pct=25.0,
    )
    rec = entry.output["loop3_recommendation"]
    primary = rec["primary_recommendation"]

    # Barbell ratio = ~2.19×
    assert rec["sa_lp_barbell_ratio"] > 2.0
    assert rec["sa_lp_barbell_ratio"] < 2.5

    # Raw implied reduction > cap → cap binds
    assert primary["raw_pct_implied_by_sa_lp_barbell"] > MAX_CASH_RAISE_PCT_OF_LONG_BOOK
    assert primary["pct_reduction_of_thematic_long_book"] == MAX_CASH_RAISE_PCT_OF_LONG_BOOK
    assert primary["cap_bound"] is True


def test_compute_includes_secondary_by_default():
    entry = compute(
        sa_lp_long_book=SA_LP_LONG_BOOK,
        sa_lp_put_complex=SA_LP_PUT_COMPLEX,
        light_street_long_book=LIGHT_STREET_LONG_BOOK,
        thematic_allocation_pct=25.0,
    )
    secondary = entry.output["loop3_recommendation"]["secondary_recommendation"]
    assert secondary is not None
    assert secondary["type"] == "nvda_otm_puts"
    assert secondary["pct_of_total_portfolio"] == NVDA_PUTS_DEFAULT_PCT_OF_TOTAL
    assert secondary["spread_quality_check_passed"] is True


def test_compute_omits_secondary_when_disabled():
    entry = compute(
        sa_lp_long_book=SA_LP_LONG_BOOK,
        sa_lp_put_complex=SA_LP_PUT_COMPLEX,
        light_street_long_book=LIGHT_STREET_LONG_BOOK,
        thematic_allocation_pct=25.0,
        include_portfolio_insurance=False,
    )
    assert entry.output["loop3_recommendation"]["secondary_recommendation"] is None
    # SMH refusal still surfaces even when no secondary requested
    refused = entry.output["loop3_recommendation"]["refused_secondary_recommendations"]
    assert any(r["type"] == "smh_otm_puts" for r in refused)


def test_compute_smh_always_refused():
    """SMH MUST be in the refused list, regardless of input — Week 1c verification."""
    entry = compute(
        sa_lp_long_book=SA_LP_LONG_BOOK,
        sa_lp_put_complex=SA_LP_PUT_COMPLEX,
        light_street_long_book=LIGHT_STREET_LONG_BOOK,
        thematic_allocation_pct=25.0,
    )
    refused = entry.output["loop3_recommendation"]["refused_secondary_recommendations"]
    smh_refusals = [r for r in refused if r["type"] == "smh_otm_puts"]
    assert len(smh_refusals) == 1
    assert "Week 1c" in smh_refusals[0]["reason"] or "spread" in smh_refusals[0]["reason"]


def test_compute_sa_lp_put_replication_always_refused():
    entry = compute(
        sa_lp_long_book=SA_LP_LONG_BOOK,
        sa_lp_put_complex=SA_LP_PUT_COMPLEX,
        light_street_long_book=LIGHT_STREET_LONG_BOOK,
        thematic_allocation_pct=25.0,
    )
    refused = entry.output["loop3_recommendation"]["refused_secondary_recommendations"]
    rep_refusals = [
        r for r in refused if r["type"] == "sa_lp_put_complex_replication"
    ]
    assert len(rep_refusals) == 1


# ---------------------------------------------------------------------------
# Cap behaviour
# ---------------------------------------------------------------------------


def test_compute_uncapped_when_barbell_below_50pct():
    """Small put complex → raw cash-raise pct should be returned uncapped."""
    small_puts = [Position("NVDA", "NVIDIA", 100.0e6)]  # ~2.5% of long book
    entry = compute(
        sa_lp_long_book=SA_LP_LONG_BOOK,
        sa_lp_put_complex=small_puts,
        light_street_long_book=LIGHT_STREET_LONG_BOOK,
        thematic_allocation_pct=25.0,
    )
    primary = entry.output["loop3_recommendation"]["primary_recommendation"]
    assert primary["cap_bound"] is False
    assert primary["pct_reduction_of_thematic_long_book"] < MAX_CASH_RAISE_PCT_OF_LONG_BOOK
    # Raw should be ~2.58% (100M/3.87B)
    assert math.isclose(
        primary["pct_reduction_of_thematic_long_book"],
        primary["raw_pct_implied_by_sa_lp_barbell"],
        rel_tol=1e-9,
    )


# ---------------------------------------------------------------------------
# Light Street cross-reference signal
# ---------------------------------------------------------------------------


def test_compute_light_street_chip_exposure_computed():
    """Light Street's chip-bullish tickers (NVDA + AVGO + MU here) form the cross-ref signal."""
    entry = compute(
        sa_lp_long_book=SA_LP_LONG_BOOK,
        sa_lp_put_complex=SA_LP_PUT_COMPLEX,
        light_street_long_book=LIGHT_STREET_LONG_BOOK,
        thematic_allocation_pct=25.0,
    )
    rec = entry.output["loop3_recommendation"]
    # Chip cluster in LS = NVDA $100M + AVGO $80M + MU $60M = $240M / $380M total ≈ 63%
    ls_total = sum(p.value_usd for p in LIGHT_STREET_LONG_BOOK)
    ls_chip = 100.0e6 + 80.0e6 + 60.0e6
    expected_pct = ls_chip / ls_total * 100.0
    assert math.isclose(
        rec["light_street_chip_exposure_pct"], expected_pct, rel_tol=1e-9
    )


def test_compute_empty_light_street_book_zero_chip_signal():
    entry = compute(
        sa_lp_long_book=SA_LP_LONG_BOOK,
        sa_lp_put_complex=SA_LP_PUT_COMPLEX,
        light_street_long_book=[],
        thematic_allocation_pct=25.0,
    )
    assert entry.output["loop3_recommendation"]["light_street_chip_exposure_pct"] == 0.0
    # And no LS source_ref entry
    refs = entry.output["loop3_recommendation"]["source_refs"]
    assert not any(r.get("kind") == "light_street_long_book_path" for r in refs)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_compute_rejects_empty_long_book():
    with pytest.raises(ValueError, match="sa_lp_long_book"):
        compute(
            sa_lp_long_book=[],
            sa_lp_put_complex=SA_LP_PUT_COMPLEX,
            light_street_long_book=LIGHT_STREET_LONG_BOOK,
            thematic_allocation_pct=25.0,
        )


def test_compute_rejects_empty_put_complex():
    """If there's no put complex, Loop 3 shouldn't have been triggered in the first place."""
    with pytest.raises(ValueError, match="put_complex"):
        compute(
            sa_lp_long_book=SA_LP_LONG_BOOK,
            sa_lp_put_complex=[],
            light_street_long_book=LIGHT_STREET_LONG_BOOK,
            thematic_allocation_pct=25.0,
        )


def test_compute_rejects_invalid_allocation():
    with pytest.raises(ValueError, match="thematic_allocation_pct"):
        compute(
            sa_lp_long_book=SA_LP_LONG_BOOK,
            sa_lp_put_complex=SA_LP_PUT_COMPLEX,
            light_street_long_book=LIGHT_STREET_LONG_BOOK,
            thematic_allocation_pct=20.0,  # not 10/15/25
        )


def test_compute_rejects_negative_value_in_book():
    bad_book = [Position("BAD", "BAD", -100.0)] + SA_LP_LONG_BOOK
    with pytest.raises(ValueError, match="non-positive"):
        compute(
            sa_lp_long_book=bad_book,
            sa_lp_put_complex=SA_LP_PUT_COMPLEX,
            light_street_long_book=LIGHT_STREET_LONG_BOOK,
            thematic_allocation_pct=25.0,
        )


# ---------------------------------------------------------------------------
# Output shape / contract
# ---------------------------------------------------------------------------


def test_output_matches_loop1_short_overlay_block_shape():
    """The output's `loop3_recommendation` dict should slot into Loop 1's
    short_overlay_bias_flag block — verify required keys."""
    entry = compute(
        sa_lp_long_book=SA_LP_LONG_BOOK,
        sa_lp_put_complex=SA_LP_PUT_COMPLEX,
        light_street_long_book=LIGHT_STREET_LONG_BOOK,
        thematic_allocation_pct=25.0,
    )
    rec = entry.output["loop3_recommendation"]
    for k in (
        "primary_recommendation",
        "secondary_recommendation",
        "refused_secondary_recommendations",
        "sa_lp_barbell_ratio",
        "light_street_chip_exposure_pct",
        "rationale_summary",
        "source_refs",
    ):
        assert k in rec, f"missing required key {k!r}"


def test_compute_rationale_includes_loop1_pass_through():
    """The Loop 1 trigger rationale should flow into the output rationale_summary."""
    entry = compute(
        sa_lp_long_book=SA_LP_LONG_BOOK,
        sa_lp_put_complex=SA_LP_PUT_COMPLEX,
        light_street_long_book=LIGHT_STREET_LONG_BOOK,
        thematic_allocation_pct=25.0,
        rationale_from_loop1="Hyper-fragile regime detected — escalate short-overlay.",
    )
    rationale = entry.output["loop3_recommendation"]["rationale_summary"]
    assert "Hyper-fragile" in rationale


def test_trace_entry_is_json_serializable():
    import json
    entry = compute(
        sa_lp_long_book=SA_LP_LONG_BOOK,
        sa_lp_put_complex=SA_LP_PUT_COMPLEX,
        light_street_long_book=LIGHT_STREET_LONG_BOOK,
        thematic_allocation_pct=25.0,
    )
    json.dumps(entry.inputs)
    json.dumps(entry.output)


def test_secondary_recommendation_respects_2pct_cap():
    """Defensive check — the constant should be ≤ MAX hard cap."""
    assert NVDA_PUTS_DEFAULT_PCT_OF_TOTAL <= MAX_NVDA_PUTS_PCT_OF_TOTAL
