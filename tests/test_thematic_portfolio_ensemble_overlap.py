"""Tests for tools.thematic_portfolio.ensemble_overlap."""
from __future__ import annotations

import math

import pytest

from tools.thematic_portfolio import Position
from tools.thematic_portfolio.ensemble_overlap import (
    compute_critic_trigger_context,
    compute_ensemble_triangulation_rank,
    compute_jaccard,
)


def _book(*items: tuple[str, float]) -> list[Position]:
    return [Position(t, t, v) for t, v in items]


# ---------------------------------------------------------------------------
# M1 — Jaccard
# ---------------------------------------------------------------------------


def test_jaccard_full_overlap():
    sa = _book(("A", 1.0), ("B", 1.0), ("C", 1.0))
    sub = _book(("A", 1.0), ("B", 1.0), ("C", 1.0))
    out = compute_jaccard(sa, sub).output
    assert out["jaccard"] == 1.0
    assert out["passes_m1_threshold"] is True
    assert out["sa_lp_only_tickers"] == []
    assert out["subagent_only_tickers"] == []


def test_jaccard_no_overlap():
    sa = _book(("A", 1.0))
    sub = _book(("B", 1.0))
    out = compute_jaccard(sa, sub).output
    assert out["jaccard"] == 0.0
    assert out["passes_m1_threshold"] is False


def test_jaccard_partial_overlap():
    sa = _book(("A", 1.0), ("B", 1.0), ("C", 1.0), ("D", 1.0))
    sub = _book(("A", 1.0), ("B", 1.0), ("E", 1.0))
    out = compute_jaccard(sa, sub).output
    # intersection {A,B} = 2; union {A,B,C,D,E} = 5; 2/5 = 0.4
    assert math.isclose(out["jaccard"], 0.4, rel_tol=1e-9)
    assert out["passes_m1_threshold"] is False
    assert out["intersection_tickers"] == ["A", "B"]
    assert out["sa_lp_only_tickers"] == ["C", "D"]
    assert out["subagent_only_tickers"] == ["E"]


def test_jaccard_threshold_boundary():
    """0.85 is the M1 pass threshold per session-2 #3."""
    # 17 out of 20 → 0.85 exactly
    sa = _book(*[(f"T{i}", 1.0) for i in range(20)])
    sub = _book(*[(f"T{i}", 1.0) for i in range(17)] + [("X", 1.0), ("Y", 1.0), ("Z", 1.0)])
    out = compute_jaccard(sa, sub).output
    # intersection = 17; union = 23; 17/23 ≈ 0.739 (NOT 0.85)
    assert math.isclose(out["jaccard"], 17 / 23, rel_tol=1e-9)
    assert out["passes_m1_threshold"] is False


def test_jaccard_empty_both_books_returns_zero():
    out = compute_jaccard([], []).output
    assert out["jaccard"] == 0.0


# ---------------------------------------------------------------------------
# M3 — rank-based ensemble triangulation
# ---------------------------------------------------------------------------


def test_triangulation_perfect_overlap():
    """If every SA LP top-K position is in the ensemble union, overlap = 1.0."""
    sa = _book(("A", 10.0), ("B", 9.0), ("C", 8.0))
    altimeter = _book(("A", 5.0), ("B", 4.0))
    coatue = _book(("C", 10.0), ("X", 9.0))
    out = compute_ensemble_triangulation_rank(
        sa,
        {"altimeter": altimeter, "coatue": coatue},
        top_k=3,
    ).output
    assert out["overlap_pct"] == 1.0
    assert out["passes_m3_threshold"] is True
    assert out["sa_lp_only_at_top_k"] == []
    assert out["weighting"] == "rank_based"


def test_triangulation_zero_overlap():
    sa = _book(("A", 10.0), ("B", 9.0))
    altimeter = _book(("X", 5.0), ("Y", 4.0))
    out = compute_ensemble_triangulation_rank(
        sa, {"altimeter": altimeter}, top_k=2
    ).output
    assert out["overlap_pct"] == 0.0
    assert out["passes_m3_threshold"] is False


def test_triangulation_partial_overlap_threshold_boundary():
    """0.5 is the M3 pass threshold per session-2 #3 (consensus health signal)."""
    sa = _book(("A", 10.0), ("B", 9.0), ("C", 8.0), ("D", 7.0))
    altimeter = _book(("A", 5.0), ("B", 4.0))
    out = compute_ensemble_triangulation_rank(
        sa, {"altimeter": altimeter}, top_k=4
    ).output
    # SA top-4 = {A,B,C,D}; altimeter top-4 = {A,B}; intersection = {A,B}; 2/4 = 0.5
    assert math.isclose(out["overlap_pct"], 0.5, rel_tol=1e-9)
    assert out["passes_m3_threshold"] is True  # ≥ 0.5 passes


def test_triangulation_rank_based_ignores_notional():
    """Light Street's $0.50B and Coatue's $29B should weight equally at the rank layer.

    Regression test for session-2 design change #4 — the whole point of rank-based
    comparison is that Light Street isn't drowned by Coatue's AUM.
    """
    sa = _book(("A", 100.0), ("B", 90.0))
    coatue_giant = _book(("A", 29_000_000_000.0), ("X", 25_000_000_000.0))
    light_street_tiny = _book(("B", 50_000_000.0))
    out = compute_ensemble_triangulation_rank(
        sa,
        {"coatue": coatue_giant, "light_street": light_street_tiny},
        top_k=2,
    ).output
    # Both A (from Coatue) and B (from Light Street) are picked up despite the
    # 580× notional gap. Overlap = 2/2 = 1.0.
    assert out["overlap_pct"] == 1.0
    # Per-fund breakdown shows Light Street DID contribute B even at tiny size.
    assert "B" in out["per_fund"]["light_street"]["overlap_with_sa_lp_top_k"]


def test_triangulation_per_fund_breakdown_shape():
    sa = _book(("A", 10.0), ("B", 9.0), ("C", 8.0))
    altimeter = _book(("A", 5.0))
    coatue = _book(("B", 5.0), ("C", 4.0))
    light_street = _book(("D", 5.0))
    out = compute_ensemble_triangulation_rank(
        sa,
        {"altimeter": altimeter, "coatue": coatue, "light_street": light_street},
        top_k=3,
    ).output
    assert out["per_fund"]["altimeter"]["overlap_count"] == 1
    assert out["per_fund"]["coatue"]["overlap_count"] == 2
    assert out["per_fund"]["light_street"]["overlap_count"] == 0


def test_triangulation_rejects_unknown_fund():
    sa = _book(("A", 1.0))
    with pytest.raises(ValueError, match="unknown ensemble fund"):
        compute_ensemble_triangulation_rank(sa, {"druckenmiller": _book(("A", 1.0))})


def test_triangulation_rejects_empty_sa_lp_book():
    with pytest.raises(ValueError, match="non-empty"):
        compute_ensemble_triangulation_rank([], {"altimeter": _book(("A", 1.0))})


def test_triangulation_handles_empty_ensemble_fund_book():
    """Edge case: Light Street Photon hasn't filed yet → empty book. Don't crash."""
    sa = _book(("A", 10.0), ("B", 9.0))
    out = compute_ensemble_triangulation_rank(
        sa, {"altimeter": _book(("A", 1.0)), "light_street": []}, top_k=2
    ).output
    assert out["per_fund"]["light_street"]["overlap_count"] == 0
    assert out["per_fund"]["altimeter"]["overlap_count"] == 1


def test_triangulation_top_k_zero_rejected():
    sa = _book(("A", 1.0))
    with pytest.raises(ValueError, match="k must be positive"):
        compute_ensemble_triangulation_rank(sa, {"altimeter": _book(("A", 1.0))}, top_k=0)


# ---------------------------------------------------------------------------
# Critic-trigger context (session-2 #5 pseudocode)
# ---------------------------------------------------------------------------


def test_trigger_ensemble_disagreement():
    """Some funds hold, some exited this quarter → ensemble_disagreement."""
    out = compute_critic_trigger_context(
        ticker="X",
        sa_lp_latest_tickers={"X"},
        sa_lp_prior_tickers={"X"},
        ensemble_latest={"altimeter": {"X"}, "coatue": set()},
        ensemble_prior={"altimeter": {"X"}, "coatue": {"X"}},
    ).output
    assert out["trigger_rule"] == "ensemble_disagreement"
    assert out["ensemble_holds"] == ["altimeter"]
    assert out["ensemble_exits"] == ["coatue"]
    assert out["conviction_tier"] == "boost"


def test_trigger_sa_lp_doubling_down_vs_consensus_exit():
    """All ensemble funds exited this quarter; SA LP NEWLY added → doubling-down."""
    out = compute_critic_trigger_context(
        ticker="X",
        sa_lp_latest_tickers={"X"},
        sa_lp_prior_tickers=set(),  # NOT in prior — newly added
        ensemble_latest={"altimeter": set(), "coatue": set()},
        ensemble_prior={"altimeter": {"X"}, "coatue": {"X"}},
    ).output
    assert out["trigger_rule"] == "sa_lp_doubling_down_vs_consensus_exit"
    assert out["ensemble_holds"] == []
    assert out["ensemble_exits"] == ["altimeter", "coatue"]
    assert out["sa_lp_added_this_quarter"] is True
    assert out["conviction_tier"] == "sa_lp_only"


def test_trigger_non_consensus_sa_lp_solo_after_consensus_exit():
    """All ensemble funds exited; SA LP still holds (not newly added) → solo carry."""
    out = compute_critic_trigger_context(
        ticker="X",
        sa_lp_latest_tickers={"X"},
        sa_lp_prior_tickers={"X"},  # SA LP held last quarter too — not newly added
        ensemble_latest={"altimeter": set()},
        ensemble_prior={"altimeter": {"X"}},
    ).output
    assert out["trigger_rule"] == "non_consensus_sa_lp_solo"
    assert out["sa_lp_added_this_quarter"] is False


def test_trigger_non_consensus_sa_lp_solo_baseline():
    """No ensemble fund holds, no ensemble fund exited → sa_lp_only baseline."""
    out = compute_critic_trigger_context(
        ticker="X",
        sa_lp_latest_tickers={"X"},
        sa_lp_prior_tickers={"X"},
        ensemble_latest={"altimeter": set(), "coatue": set()},
        ensemble_prior={"altimeter": set(), "coatue": set()},
    ).output
    assert out["trigger_rule"] == "non_consensus_sa_lp_solo"
    assert out["ensemble_holds"] == []
    assert out["ensemble_exits"] == []


def test_trigger_consensus_position():
    """Held by SA LP + ≥1 ensemble fund; no recent exits → none (baseline)."""
    out = compute_critic_trigger_context(
        ticker="X",
        sa_lp_latest_tickers={"X"},
        sa_lp_prior_tickers={"X"},
        ensemble_latest={"altimeter": {"X"}, "coatue": {"X"}},
        ensemble_prior={"altimeter": {"X"}, "coatue": {"X"}},
    ).output
    assert out["trigger_rule"] == "none"
    assert out["conviction_tier"] == "boost"
    assert out["ensemble_exits"] == []


def test_trigger_missing_prior_fund_treats_as_empty():
    """If a fund has no prior entry (e.g. Photon's first filing), no exit fires."""
    out = compute_critic_trigger_context(
        ticker="X",
        sa_lp_latest_tickers={"X"},
        sa_lp_prior_tickers={"X"},
        ensemble_latest={"light_street": {"X"}},
        ensemble_prior={},  # no prior at all
    ).output
    assert out["ensemble_exits"] == []
    assert out["trigger_rule"] == "none"


def test_trigger_rejects_unknown_fund_in_latest():
    with pytest.raises(ValueError, match="unknown ensemble fund"):
        compute_critic_trigger_context(
            ticker="X",
            sa_lp_latest_tickers={"X"},
            sa_lp_prior_tickers={"X"},
            ensemble_latest={"druckenmiller": {"X"}},
            ensemble_prior={},
        )


def test_trigger_context_summary_includes_ticker():
    """Sanity check the human-readable summary mentions the ticker."""
    out = compute_critic_trigger_context(
        ticker="SNDK",
        sa_lp_latest_tickers={"SNDK"},
        sa_lp_prior_tickers={"SNDK"},
        ensemble_latest={"altimeter": set()},
        ensemble_prior={"altimeter": set()},
    ).output
    assert "SNDK" in out["context_summary"]
