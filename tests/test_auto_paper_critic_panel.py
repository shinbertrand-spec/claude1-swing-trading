"""Tests for tools.auto_paper.critic_panel — Phase 3 multi-rater aggregator.

The aggregator is pure-Python deterministic with 4 priority rules. Tests
cover all 4 rule branches plus edge cases (single vote, ties, missing
fields, invalid adjustment values) and verify the JSONSchema for
PanelVerdict.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tools.auto_paper import critic_panel as cp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schema():
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "ledgers" / "swing-critics" / "_schema" / "panel.schema.json"
    )
    with open(schema_path, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def validator(schema):
    return Draft202012Validator(schema)


def _vote(critic: str, adjustment: str, *, ticker="VRT", cost=0.10) -> cp.CriticVote:
    return cp.CriticVote(
        critic=critic,
        candidate_ticker=ticker,
        panel_call_id=f"2026-05-27T22-15__{ticker}",
        panel_firing_date="2026-05-27",
        risks=[{"risk": "test", "grounding_evidence": "test", "severity": "medium"}],
        confidence_adjustment=adjustment,
        adjustment_rationale=f"{critic} flags {adjustment}",
        estimated_cost_usd=cost,
    )


# ---------------------------------------------------------------------------
# Rule 4: preserve (all-hold or single minus_20)
# ---------------------------------------------------------------------------


def test_all_hold_preserves(validator):
    votes = [
        _vote("risk_manager", "hold"),
        _vote("setup_quality_hawk", "hold"),
        _vote("macro_skeptic", "hold"),
    ]
    v = cp.aggregate_panel(votes, ticker="VRT", panel_call_id="test-1")
    assert v.action == "preserve"
    assert v.sizing_multiplier == 1.0
    assert v.n_critics_hold == 3
    assert v.n_critics_minus_20 == 0
    assert v.minus_20_critics == []
    assert v.total_cost_usd == 0.30
    errors = list(validator.iter_errors(v.to_dict()))
    assert errors == [], f"Schema errors: {errors}"


def test_single_minus_20_still_preserves():
    """Rule 3 requires ≥2 minus_20. A single minus_20 stays preserve."""
    votes = [
        _vote("risk_manager", "minus_20"),
        _vote("setup_quality_hawk", "hold"),
        _vote("macro_skeptic", "hold"),
    ]
    v = cp.aggregate_panel(votes, ticker="VRT", panel_call_id="test-2")
    assert v.action == "preserve"
    assert v.sizing_multiplier == 1.0
    assert v.n_critics_minus_20 == 1
    assert "risk_manager" in v.minus_20_critics
    assert "below ≥2 threshold" in v.rationale


# ---------------------------------------------------------------------------
# Rule 3: ≥2 minus_20 → reduce 20%
# ---------------------------------------------------------------------------


def test_two_minus_20_reduces_20(validator):
    votes = [
        _vote("risk_manager", "minus_20"),
        _vote("setup_quality_hawk", "minus_20"),
        _vote("macro_skeptic", "hold"),
    ]
    v = cp.aggregate_panel(votes, ticker="VRT", panel_call_id="test-3")
    assert v.action == "reduce_20"
    assert v.sizing_multiplier == 0.8
    assert v.n_critics_minus_20 == 2
    assert set(v.minus_20_critics) == {"risk_manager", "setup_quality_hawk"}
    errors = list(validator.iter_errors(v.to_dict()))
    assert errors == []


def test_three_minus_20_still_reduces_20():
    """≥2 threshold; sizing is fixed 0.8 regardless of count above 2."""
    votes = [
        _vote("risk_manager", "minus_20"),
        _vote("setup_quality_hawk", "minus_20"),
        _vote("macro_skeptic", "minus_20"),
    ]
    v = cp.aggregate_panel(votes, ticker="VRT", panel_call_id="test-3b")
    assert v.action == "reduce_20"
    assert v.sizing_multiplier == 0.8
    assert v.n_critics_minus_20 == 3


# ---------------------------------------------------------------------------
# Rule 2: any minus_50 → half-size + review
# ---------------------------------------------------------------------------


def test_single_minus_50_half_sizes(validator):
    votes = [
        _vote("risk_manager", "hold"),
        _vote("setup_quality_hawk", "minus_50"),
        _vote("macro_skeptic", "hold"),
    ]
    v = cp.aggregate_panel(votes, ticker="VRT", panel_call_id="test-4")
    assert v.action == "half_size_review"
    assert v.sizing_multiplier == 0.5
    assert v.n_critics_minus_50 == 1
    assert "setup_quality_hawk" in v.minus_50_critics
    errors = list(validator.iter_errors(v.to_dict()))
    assert errors == []


def test_minus_50_overrides_minus_20s():
    """Rule 2 short-circuits before Rule 3 — any minus_50 wins."""
    votes = [
        _vote("risk_manager", "minus_20"),
        _vote("setup_quality_hawk", "minus_50"),
        _vote("macro_skeptic", "minus_20"),
    ]
    v = cp.aggregate_panel(votes, ticker="VRT", panel_call_id="test-5")
    assert v.action == "half_size_review"
    assert v.sizing_multiplier == 0.5
    assert v.n_critics_minus_50 == 1
    assert v.n_critics_minus_20 == 2  # still counted


# ---------------------------------------------------------------------------
# Rule 1: any structural_risk → defer (highest priority)
# ---------------------------------------------------------------------------


def test_structural_risk_defers(validator):
    votes = [
        _vote("risk_manager", "structural_risk"),
        _vote("setup_quality_hawk", "hold"),
        _vote("macro_skeptic", "hold"),
    ]
    v = cp.aggregate_panel(votes, ticker="VRT", panel_call_id="test-6")
    assert v.action == "defer"
    assert v.sizing_multiplier == 0.0
    assert v.n_critics_structural_risk == 1
    errors = list(validator.iter_errors(v.to_dict()))
    assert errors == []


def test_structural_risk_overrides_minus_50():
    """Rule 1 short-circuits before Rules 2 & 3."""
    votes = [
        _vote("risk_manager", "structural_risk"),
        _vote("setup_quality_hawk", "minus_50"),
        _vote("macro_skeptic", "minus_20"),
    ]
    v = cp.aggregate_panel(votes, ticker="VRT", panel_call_id="test-7")
    assert v.action == "defer"
    assert v.sizing_multiplier == 0.0
    assert v.n_critics_structural_risk == 1
    assert v.n_critics_minus_50 == 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_votes_raises():
    with pytest.raises(ValueError, match="at least one"):
        cp.aggregate_panel([], ticker="VRT", panel_call_id="test-8")


def test_invalid_adjustment_raises():
    bad = cp.CriticVote(
        critic="risk_manager",
        candidate_ticker="VRT",
        panel_call_id="x", panel_firing_date="2026-05-27",
        risks=[], confidence_adjustment="bogus",
        adjustment_rationale="", estimated_cost_usd=0.0,
    )
    with pytest.raises(ValueError, match="invalid confidence_adjustment"):
        cp.aggregate_panel([bad], ticker="VRT", panel_call_id="test-9")


def test_single_vote_hold_preserves():
    """One critic, hold — preserve, but n_critics_total=1."""
    votes = [_vote("risk_manager", "hold")]
    v = cp.aggregate_panel(votes, ticker="VRT", panel_call_id="test-10")
    assert v.action == "preserve"
    assert v.n_critics_total == 1


def test_shadow_mode_flag_threads_through():
    votes = [_vote("risk_manager", "hold")]
    v_shadow = cp.aggregate_panel(
        votes, ticker="VRT", panel_call_id="t", shadow_mode=True,
    )
    v_live = cp.aggregate_panel(
        votes, ticker="VRT", panel_call_id="t", shadow_mode=False,
    )
    assert v_shadow.shadow_mode is True
    assert v_live.shadow_mode is False
    # Multiplier is computed identically; downstream consumers decide
    # whether to apply it based on shadow_mode flag.
    assert v_shadow.sizing_multiplier == v_live.sizing_multiplier


def test_cost_aggregation():
    """total_cost_usd sums estimated_cost_usd across all critics."""
    votes = [
        _vote("risk_manager", "hold", cost=0.07),
        _vote("setup_quality_hawk", "minus_20", cost=0.12),
        _vote("macro_skeptic", "hold", cost=0.09),
    ]
    v = cp.aggregate_panel(votes, ticker="VRT", panel_call_id="t")
    assert v.total_cost_usd == pytest.approx(0.28)


# ---------------------------------------------------------------------------
# CriticVote.from_dict — accepts either grounding_evidence or grounding_citation
# ---------------------------------------------------------------------------


def test_from_dict_swing_critic_shape():
    """Swing critics emit grounding_evidence."""
    d = {
        "critic": "risk_manager",
        "candidate_ticker": "VRT",
        "panel_call_id": "test",
        "panel_firing_date": "2026-05-27",
        "risks": [{
            "risk": "Concentration with human-track VRT",
            "grounding_evidence": "portfolio_context.existing_positions[2].ticker=VRT",
            "severity": "high",
        }],
        "confidence_adjustment": "minus_20",
        "adjustment_rationale": "test",
        "estimated_cost_usd": 0.10,
    }
    v = cp.CriticVote.from_dict(d)
    assert v.risks[0]["grounding_evidence"] == "portfolio_context.existing_positions[2].ticker=VRT"
    # Both fields populated for downstream consumer convenience
    assert v.risks[0]["grounding_citation"] == "portfolio_context.existing_positions[2].ticker=VRT"


def test_from_dict_thematic_critic_shape():
    """Reused thematic critics (Patel/Rasgon) emit grounding_citation."""
    d = {
        "critic": "patel",
        "position_ticker": "MXL",  # thematic-style key
        "critic_call_id": "test",   # thematic-style key
        "risks": [{
            "risk": "Memory cycle inversion",
            "grounding_citation": "Patel Substack 2025-Q4: HBM oversupply 2027H1",
            "severity": "high",
        }],
        "confidence_adjustment": "minus_50",
        "adjustment_rationale": "test",
        "estimated_cost_usd": 0.10,
    }
    v = cp.CriticVote.from_dict(d)
    assert v.candidate_ticker == "MXL"
    assert v.panel_call_id == "test"
    # Both fields populated even though only one was provided
    assert v.risks[0]["grounding_evidence"] == "Patel Substack 2025-Q4: HBM oversupply 2027H1"
    assert v.risks[0]["grounding_citation"] == "Patel Substack 2025-Q4: HBM oversupply 2027H1"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def test_save_critic_vote_writes_file(tmp_path):
    vote = _vote("risk_manager", "hold")
    path = cp.save_critic_vote(
        vote, ledger_date=date(2026, 5, 27), panel_dir=tmp_path,
    )
    assert path.exists()
    with open(path, encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert loaded["critic"] == "risk_manager"
    assert loaded["candidate_ticker"] == "VRT"


def test_save_panel_verdict_writes_file(tmp_path):
    votes = [_vote("risk_manager", "hold")]
    v = cp.aggregate_panel(votes, ticker="VRT", panel_call_id="t")
    path = cp.save_panel_verdict(
        v, ledger_date=date(2026, 5, 27), panel_dir=tmp_path,
    )
    assert path.exists()
    assert path.name == "_panel.json"
    with open(path, encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert loaded["ticker"] == "VRT"
    assert loaded["action"] == "preserve"


def test_append_calibration_log(tmp_path):
    votes = [_vote("risk_manager", "hold")]
    v = cp.aggregate_panel(votes, ticker="VRT", panel_call_id="t")
    path = cp.append_calibration_log(
        v, placement_status="placed", placement_shares=152,
        ledger_date=date(2026, 5, 27), panel_dir=tmp_path,
    )
    assert path.exists()
    # Append-only — second call adds a second line
    cp.append_calibration_log(
        v, placement_status="placed", placement_shares=100,
        ledger_date=date(2026, 5, 27), panel_dir=tmp_path,
    )
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    line1 = json.loads(lines[0])
    line2 = json.loads(lines[1])
    assert line1["placement_shares"] == 152
    assert line2["placement_shares"] == 100


# ---------------------------------------------------------------------------
# Realistic scenarios (mirror the 4 live picks from 2026-05-26)
# ---------------------------------------------------------------------------


def test_scenario_vrt_distribution_character():
    """VRT — 5/5 fundamentals + Stage 2 + Pullback-20SMA setup.
    Setup-Quality Hawk flags distribution character.
    Risk Manager flags overlap with human-track VRT.
    Macro Skeptic holds (regime supportive).
    Expected: 2 minus_20 → reduce_20 (sizing_multiplier 0.8)."""
    votes = [
        _vote("risk_manager", "minus_20"),
        _vote("setup_quality_hawk", "minus_20"),
        _vote("macro_skeptic", "hold"),
    ]
    v = cp.aggregate_panel(votes, ticker="VRT", panel_call_id="vrt-scenario")
    assert v.action == "reduce_20"
    assert v.sizing_multiplier == 0.8


def test_scenario_intu_stage_4_post_earnings():
    """INTU — Stage 4 (0/8), post-earnings dislocation, xs_short_term_reversal.
    Setup-Quality Hawk: minus_20 (Stage 4 is by-design for this strategy).
    Macro Skeptic: minus_50 (post-earnings, broad market Stage 2 but candidate
    is in active distribution).
    Expected: any minus_50 wins → half_size_review (sizing_multiplier 0.5)."""
    votes = [
        _vote("risk_manager", "hold"),
        _vote("setup_quality_hawk", "minus_20"),
        _vote("macro_skeptic", "minus_50"),
    ]
    v = cp.aggregate_panel(votes, ticker="INTU", panel_call_id="intu-scenario")
    assert v.action == "half_size_review"
    assert v.sizing_multiplier == 0.5
    assert "macro_skeptic" in v.minus_50_critics


def test_scenario_mxl_pt_inversion_quant_insight():
    """MXL — analyst PT inversion + clenow_momentum top pick + signal_rank=1 of 5.
    Setup-Quality Hawk: minus_50 (consensus PT 49% below entry).
    Quant Insight: hold (signal_rank=1, top of cohort).
    Risk Manager: hold.
    Macro Skeptic: hold.
    Expected: any minus_50 wins → half_size_review."""
    votes = [
        _vote("risk_manager", "hold"),
        _vote("setup_quality_hawk", "minus_50"),
        _vote("macro_skeptic", "hold"),
        _vote("quant_insight", "hold"),
    ]
    v = cp.aggregate_panel(votes, ticker="MXL", panel_call_id="mxl-scenario")
    assert v.action == "half_size_review"
    assert v.sizing_multiplier == 0.5
