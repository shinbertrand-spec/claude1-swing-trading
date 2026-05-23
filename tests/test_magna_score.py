"""Tests for tools.magna_score."""
from __future__ import annotations

from tools.magna_score import compute


def test_full_5_score_smci_like():
    """Golden-EP candidate: 5/5 MAGNA, like the SMCI example fixture."""
    e = compute(
        eps_yoy_growth=3.92,
        sales_yoy_growth=1.74,
        after_hours_gap_pct=0.07,
        premarket_volume_shares=380_000,
        gap_confirmed_regular_session=True,
        neglected=True,
        analyst_upgrades=True,
    )
    assert e.output["magna_score"] == 5
    assert e.output["golden_ep_eligible"] is True
    for letter in (
        "M_massive_earnings",
        "A_after_hours_gap",
        "G_gap_confirmed",
        "N_neglected",
        "A_analyst_upgrades",
    ):
        assert e.output["breakdown"][letter]["pass"] is True


def test_m_passes_with_eps_only():
    e = compute(
        eps_yoy_growth=1.5,  # 150% — over threshold
        sales_yoy_growth=0.10,
        after_hours_gap_pct=None,
        premarket_volume_shares=None,
        gap_confirmed_regular_session=False,
        neglected=False,
        analyst_upgrades=False,
    )
    assert e.output["breakdown"]["M_massive_earnings"]["pass"] is True
    assert e.output["magna_score"] == 1


def test_m_passes_with_sales_only():
    e = compute(
        eps_yoy_growth=0.10,
        sales_yoy_growth=2.0,  # 200%
        after_hours_gap_pct=None,
        premarket_volume_shares=None,
        gap_confirmed_regular_session=False,
        neglected=False,
        analyst_upgrades=False,
    )
    assert e.output["breakdown"]["M_massive_earnings"]["pass"] is True


def test_after_hours_requires_both_conditions():
    """Gap large enough but volume too small → A_after_hours fails."""
    e = compute(
        eps_yoy_growth=None,
        sales_yoy_growth=None,
        after_hours_gap_pct=0.08,
        premarket_volume_shares=50_000,  # below 100k threshold
        gap_confirmed_regular_session=False,
        neglected=False,
        analyst_upgrades=False,
    )
    assert e.output["breakdown"]["A_after_hours_gap"]["pass"] is False
    assert e.output["magna_score"] == 0


def test_all_unknown_yields_zero():
    e = compute(
        eps_yoy_growth=None,
        sales_yoy_growth=None,
        after_hours_gap_pct=None,
        premarket_volume_shares=None,
        gap_confirmed_regular_session=False,
        neglected=False,
        analyst_upgrades=False,
    )
    assert e.output["magna_score"] == 0
    assert e.output["golden_ep_eligible"] is False
