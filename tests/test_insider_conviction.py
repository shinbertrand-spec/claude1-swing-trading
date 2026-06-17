"""Tests for tools.fundamentals.insider_conviction — conviction composite."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.fundamentals.insider_conviction import (
    ConvictionLevel,
    SizeBucket,
    cluster_purchases,
    compute_conviction,
    _best_tier,
)


def _ev(cik, *, shares=1000, value=10000.0, ev="2026-06-10", officer=True,
        director=False, ten=False, title="CEO", name="Ins"):
    return SimpleNamespace(
        insider_cik=cik, shares=shares, value=value, event_date=ev,
        is_officer=officer, is_director=director, is_ten_pct_owner=ten,
        officer_title=title, position=title, insider_name=name,
    )


# ---- exclusion gate -----------------------------------------------------


def test_pure_ten_pct_owner_only_excluded():
    ev = _ev("1", officer=False, director=False, ten=True, title=None, name="BANK OF AMERICA")
    s = compute_conviction("ADTX", [ev], shares_outstanding=1_000_000)
    assert s.level == ConvictionLevel.EXCLUDE.value
    assert s.n_effective_events == 0
    assert "BANK OF AMERICA" in s.excluded_ten_pct_only


def test_ten_pct_owner_who_is_also_director_counts():
    ev = _ev("1", officer=False, director=True, ten=True)
    s = compute_conviction("X", [ev], shares_outstanding=1_000_000)
    assert s.n_effective_events == 1
    assert s.level != ConvictionLevel.EXCLUDE.value


# ---- cluster ------------------------------------------------------------


def test_cluster_two_distinct_insiders():
    evs = [_ev("1", shares=200), _ev("2", shares=200, ev="2026-06-11")]
    s = compute_conviction("X", evs, shares_outstanding=1_000_000,
                           insider_tiers={"1": "elite"})
    assert s.is_cluster is True
    assert s.n_distinct_insiders == 2
    assert s.multipliers["cluster"] == 1.5


def test_same_insider_twice_not_cluster():
    evs = [_ev("1", shares=100), _ev("1", shares=100, ev="2026-06-11")]
    s = compute_conviction("X", evs, shares_outstanding=1_000_000)
    assert s.n_distinct_insiders == 1
    assert s.is_cluster is False


# ---- size buckets -------------------------------------------------------


def test_size_high_bucket():
    # 400 / 1e6 = 0.04% >= 0.028% → HIGH
    evs = [_ev("1", shares=400)]
    s = compute_conviction("X", evs, shares_outstanding=1_000_000)
    assert s.size_bucket == SizeBucket.HIGH.value
    assert s.multipliers["size"] == 1.6


def test_size_negligible_bucket():
    # 1000 / 1e9 = 0.0001% < 0.004% → negligible
    evs = [_ev("1", shares=1000)]
    s = compute_conviction("X", evs, shares_outstanding=1_000_000_000)
    assert s.size_bucket == SizeBucket.NEGLIGIBLE.value
    assert s.multipliers["size"] == 0.3


def test_size_unknown_without_shares_outstanding():
    s = compute_conviction("X", [_ev("1")], shares_outstanding=None)
    assert s.size_bucket == SizeBucket.UNKNOWN.value
    assert s.multipliers["size"] == 1.0
    assert s.pct_shares_outstanding is None


# ---- role / tier / rnd multipliers -------------------------------------


def test_cfo_tilt():
    evs = [_ev("1", title="Chief Financial Officer", shares=100)]
    s = compute_conviction("X", evs, shares_outstanding=1_000_000)
    assert s.has_cfo is True
    assert s.multipliers["cfo"] == 1.2


def test_tier_poor_downweights():
    evs = [_ev("1", shares=100)]
    s = compute_conviction("X", evs, shares_outstanding=1_000_000,
                           insider_tiers={"1": "poor"})
    assert s.best_tier == "poor"
    assert s.multipliers["tier"] == 0.4


def test_rnd_amplifier():
    evs = [_ev("1", shares=100)]
    s = compute_conviction("X", evs, shares_outstanding=1_000_000,
                           rnd_intensity=0.25)
    assert s.rnd_amplified is True
    assert s.multipliers["rnd"] == 1.2


def test_rnd_below_threshold_no_amplify():
    s = compute_conviction("X", [_ev("1")], shares_outstanding=1_000_000,
                           rnd_intensity=0.05)
    assert s.rnd_amplified is False
    assert s.multipliers["rnd"] == 1.0


# ---- composite → level --------------------------------------------------


def test_high_conviction_stacks_to_high():
    # cluster ×1.5 · size HIGH ×1.6 · elite ×1.5 = 3.6 → HIGH
    evs = [_ev("1", shares=200), _ev("2", shares=200, ev="2026-06-11")]
    s = compute_conviction("X", evs, shares_outstanding=1_000_000,
                           insider_tiers={"1": "elite", "2": "good"})
    assert s.composite_score == pytest.approx(3.6)
    assert s.level == ConvictionLevel.HIGH.value


def test_medium_level():
    # single good-tier insider, normal size → 1.2 → MEDIUM
    evs = [_ev("1", shares=100)]
    s = compute_conviction("X", evs, shares_outstanding=1_000_000,
                           insider_tiers={"1": "good"})
    assert s.composite_score == pytest.approx(1.2)
    assert s.level == ConvictionLevel.MEDIUM.value


def test_low_level():
    # unrated single insider, negligible size → 0.8 × 0.3 = 0.24 → LOW
    evs = [_ev("1", shares=10)]
    s = compute_conviction("X", evs, shares_outstanding=1_000_000_000)
    assert s.level == ConvictionLevel.LOW.value


# ---- clustering helper --------------------------------------------------


def test_cluster_purchases_groups_by_window():
    ps = [
        _ev("1", ev="2026-06-10"),
        _ev("2", ev="2026-06-11"),   # within 2 of 06-10 → cluster 1
        _ev("3", ev="2026-06-20"),   # >2 days → cluster 2
        _ev("4", ev="2026-06-21"),   # within 2 of 06-20 → cluster 2
    ]
    clusters = cluster_purchases(ps, window_days=2)
    assert len(clusters) == 2
    assert len(clusters[0]) == 2
    assert len(clusters[1]) == 2


def test_cluster_purchases_drops_undated():
    ps = [_ev("1", ev="2026-06-10"), _ev("2", ev=None)]
    clusters = cluster_purchases(ps)
    assert sum(len(c) for c in clusters) == 1


# ---- best tier ----------------------------------------------------------


def test_best_tier_ordering():
    assert _best_tier(["poor", "good", "unrated"]) == "good"
    assert _best_tier(["poor", "unrated"]) == "unrated"
    assert _best_tier(["elite", "poor"]) == "elite"
    assert _best_tier([]) == "unrated"
