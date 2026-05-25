"""Tests for tools.thematic_portfolio.sizer.

Worked-example test cases are parametrized against the Q1 2026 SA LP 13F as
documented in the session-2 design changes (vault note) and project memory.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from tools.thematic_portfolio import Position
from tools.thematic_portfolio.sizer import (
    DEFAULT_HARD_CAP_PCT,
    compute,
    load_long_book_from_json,
)


# A representative-but-not-exhaustive Q1 2026 SA LP long-book slice for tests.
# Values calibrated to reproduce the worked example from
# swing-thematic-portfolio-session-2-design-changes §1:
#   BE @ 22.8% of long book → 5.7% raw at 25% → capped at 5.0%
#   SNDK @ 18.8% → 4.7%
#   CRWV @ 14.4% → 3.6%
#   IREN @ 10.4% → 2.6%
#   CORZ @ 10.1% → 2.5%
#   APLD @ 8.3%  → 2.07%
#   RIOT @ 3.7%  → 0.92%
# We synthesize seven holdings whose value_usd ratios match these weights.
# Use round numbers for arithmetic clarity; the percentages below match the
# spec to 3 decimal places.
SA_LP_Q1_2026_SLICE: list[Position] = [
    Position("BE",   "BLOOM ENERGY CORP",    228_000_000.0),
    Position("SNDK", "SANDISK CORP",         188_000_000.0),
    Position("CRWV", "COREWEAVE INC",        144_000_000.0),
    Position("IREN", "IREN LTD",             104_000_000.0),
    Position("CORZ", "CORE SCIENTIFIC INC",  101_000_000.0),
    Position("APLD", "APPLIED DIGITAL CORP",  83_000_000.0),
    Position("RIOT", "RIOT PLATFORMS INC",    37_000_000.0),
    # Tail of 14 smaller positions totalling ~115M, evenly split for the test.
    *[
        Position(f"TAIL{i}", f"TAIL CO {i}", 115_000_000.0 / 14)
        for i in range(14)
    ],
]
SA_LP_Q1_2026_TOTAL_USD = sum(p.value_usd for p in SA_LP_Q1_2026_SLICE)


def test_q1_2026_worked_example_at_25pct_allocation():
    """Mirror the spec's worked example: 25% thematic allocation.

    BE hits the 5% cap; SNDK / CRWV / IREN / CORZ / APLD / RIOT take their
    natural mirror weights; top-6 cluster ≈ 84% of the thematic bucket.
    """
    entry = compute(sa_lp_long_book=SA_LP_Q1_2026_SLICE, thematic_allocation_pct=25.0)
    out = entry.output
    by_ticker = {p["ticker"]: p for p in out["positions"]}

    # BE — should cap at exactly 5.0%
    be = by_ticker["BE"]
    assert be["cap_binding"] == "total_portfolio_5pct"
    assert math.isclose(be["target_weight_pct_of_total"], 5.0, rel_tol=1e-9)
    # Pre-cap raw should exceed 5.0
    assert be["raw_target_pct_of_total_pre_cap"] > 5.0

    # SNDK / CRWV / IREN / CORZ — all should be uncapped (raw < 5.0)
    for tkr in ["SNDK", "CRWV", "IREN", "CORZ", "APLD", "RIOT"]:
        assert by_ticker[tkr]["cap_binding"] == "none", f"{tkr} should not be capped"

    # Summary checks
    assert out["summary"]["n_positions"] == len(SA_LP_Q1_2026_SLICE)
    assert out["summary"]["n_cap_hits"] == 1  # only BE
    # Top-6 cluster should consume the bulk of the 25% bucket (≈ 84% per spec)
    assert out["summary"]["top_6_share_of_bucket_pct"] > 75.0
    assert out["summary"]["top_6_share_of_bucket_pct"] < 90.0


def test_position_count_preserved_through_sizing():
    entry = compute(sa_lp_long_book=SA_LP_Q1_2026_SLICE, thematic_allocation_pct=10.0)
    assert len(entry.output["positions"]) == len(SA_LP_Q1_2026_SLICE)


def test_positions_sorted_descending_by_target_weight():
    entry = compute(sa_lp_long_book=SA_LP_Q1_2026_SLICE, thematic_allocation_pct=25.0)
    weights = [p["target_weight_pct_of_total"] for p in entry.output["positions"]]
    assert weights == sorted(weights, reverse=True)


@pytest.mark.parametrize(
    "allocation_pct,expected_cap_hits",
    [
        (10.0, 0),   # 22.8% × 10% = 2.28% — no cap
        (15.0, 0),   # 22.8% × 15% = 3.42% — no cap
        (25.0, 1),   # 22.8% × 25% = 5.7%  — BE hits cap
    ],
)
def test_cap_hits_scale_with_allocation(allocation_pct, expected_cap_hits):
    entry = compute(
        sa_lp_long_book=SA_LP_Q1_2026_SLICE, thematic_allocation_pct=allocation_pct
    )
    assert entry.output["summary"]["n_cap_hits"] == expected_cap_hits


def test_thematic_bucket_consumption_under_100pct():
    """Sum of capped weights must be ≤ thematic allocation (cap can only shrink)."""
    for alloc in (10.0, 15.0, 25.0):
        entry = compute(sa_lp_long_book=SA_LP_Q1_2026_SLICE, thematic_allocation_pct=alloc)
        assert entry.output["summary"]["sum_capped_target_pct"] <= alloc + 1e-9


def test_no_capping_when_no_position_above_cap_pct_of_long_book():
    """If no position is > 20% of SA LP long book and allocation = 25%, no caps fire."""
    # Build a book where the largest position is 19.9% of total.
    book = [
        Position("A", "A", 199.0),
        Position("B", "B", 100.0),
        Position("C", "C", 100.0),
        Position("D", "D", 100.0),
        Position("E", "E", 100.0),
        Position("F", "F", 100.0),
        Position("G", "G", 100.0),
        Position("H", "H", 100.0),
        Position("I", "I", 101.0),
    ]
    entry = compute(sa_lp_long_book=book, thematic_allocation_pct=25.0)
    assert entry.output["summary"]["n_cap_hits"] == 0


def test_invalid_allocation_rejected():
    with pytest.raises(ValueError, match="thematic_allocation_pct"):
        compute(sa_lp_long_book=SA_LP_Q1_2026_SLICE, thematic_allocation_pct=20.0)


def test_empty_book_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        compute(sa_lp_long_book=[], thematic_allocation_pct=25.0)


def test_hard_cap_above_5pct_rejected():
    with pytest.raises(ValueError, match="hard_cap_pct"):
        compute(
            sa_lp_long_book=SA_LP_Q1_2026_SLICE,
            thematic_allocation_pct=25.0,
            hard_cap_pct=6.0,
        )


def test_hard_cap_nonpositive_rejected():
    with pytest.raises(ValueError, match="hard_cap_pct"):
        compute(
            sa_lp_long_book=SA_LP_Q1_2026_SLICE,
            thematic_allocation_pct=25.0,
            hard_cap_pct=0.0,
        )


def test_negative_position_value_rejected():
    bad_book = [Position("BAD", "BAD CO", -100.0)] + SA_LP_Q1_2026_SLICE
    with pytest.raises(ValueError, match="non-positive value_usd"):
        compute(sa_lp_long_book=bad_book, thematic_allocation_pct=25.0)


def test_mirror_multiplier_zero_rejected():
    with pytest.raises(ValueError, match="mirror_multiplier"):
        compute(
            sa_lp_long_book=SA_LP_Q1_2026_SLICE,
            thematic_allocation_pct=25.0,
            mirror_multiplier=0.0,
        )


def test_mirror_multiplier_below_1_shrinks_all_weights():
    """Sensitivity check: a 0.5× multiplier halves every position weight."""
    full = compute(sa_lp_long_book=SA_LP_Q1_2026_SLICE, thematic_allocation_pct=10.0)
    half = compute(
        sa_lp_long_book=SA_LP_Q1_2026_SLICE,
        thematic_allocation_pct=10.0,
        mirror_multiplier=0.5,
    )
    full_by_ticker = {p["ticker"]: p for p in full.output["positions"]}
    half_by_ticker = {p["ticker"]: p for p in half.output["positions"]}
    # No cap-binding at 10% × 22.8% so every position halves exactly.
    for tkr, full_p in full_by_ticker.items():
        assert math.isclose(
            half_by_ticker[tkr]["target_weight_pct_of_total"],
            full_p["target_weight_pct_of_total"] / 2.0,
            rel_tol=1e-9,
        )


def test_default_hard_cap_is_5pct():
    """CLAUDE.md hard rule pinned via the module constant."""
    assert DEFAULT_HARD_CAP_PCT == 5.0


def test_trace_entry_inputs_are_round_trippable():
    """TraceEntry.inputs must be JSON-serialisable per the Phase 4 contract."""
    entry = compute(sa_lp_long_book=SA_LP_Q1_2026_SLICE, thematic_allocation_pct=15.0)
    json.dumps(entry.inputs)
    json.dumps(entry.output)


def test_load_long_book_from_json_roundtrip(tmp_path: Path):
    """The JSON loader accepts the canonical row shape and recovers Position list."""
    rows = [
        {"ticker": "BE", "issuer_name": "BLOOM ENERGY", "value_usd": 228000000.0, "cusip": "093712107"},
        {"ticker": "SNDK", "issuer_name": "SANDISK", "value_usd": 188000000.0},
    ]
    path = tmp_path / "long_book.json"
    path.write_text(json.dumps(rows))
    book = load_long_book_from_json(path)
    assert [p.ticker for p in book] == ["BE", "SNDK"]
    assert book[0].cusip == "093712107"
    assert book[1].cusip is None


def test_load_long_book_from_json_rejects_missing_fields(tmp_path: Path):
    rows = [{"ticker": "BE", "issuer_name": "BLOOM ENERGY"}]  # missing value_usd
    path = tmp_path / "long_book.json"
    path.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="missing fields"):
        load_long_book_from_json(path)


def test_load_long_book_from_json_rejects_non_array(tmp_path: Path):
    path = tmp_path / "long_book.json"
    path.write_text(json.dumps({"not": "an array"}))
    with pytest.raises(ValueError, match="must contain a JSON array"):
        load_long_book_from_json(path)
