"""Tests for tools.cluster_concentration — the theme/cluster correlation cap."""
from __future__ import annotations

import math

import pytest

from tools.cluster_concentration import (
    DEFAULT_CLUSTER_CAP_PCT,
    compute,
    compute_from_books,
    load_theme_map,
    position_value,
    sum_cluster_cost,
    theme_of,
)

# A small fixture map for the arithmetic tests (independent of the real YAML).
FIXTURE_MAP = {
    "NVDA": "AI-momentum",
    "MRVL": "AI-momentum",
    "CEG": "AI-momentum",   # power — different sector, same theme
    "DLR": "AI-momentum",   # DC REIT — different sector, same theme
    "XOM": "Energy",
}


def _pos(ticker, shares, price, stage="trailing"):
    return {"ticker": ticker, "shares": shares, "entry_price": price, "stage": stage}


def test_theme_of_resolves_and_misses():
    assert theme_of("nvda", FIXTURE_MAP) == "AI-momentum"
    assert theme_of("XOM", FIXTURE_MAP) == "Energy"
    assert theme_of("WCC", FIXTURE_MAP) is None  # untagged


def test_position_value_and_open_filter():
    assert position_value(_pos("NVDA", 10, 100.0)) == 1000.0
    # closed stages excluded from the cluster sum
    books = [[_pos("NVDA", 10, 100.0, stage="closed"),
              _pos("MRVL", 10, 50.0, stage="closed_unfilled")]]
    assert sum_cluster_cost("AI-momentum", books, FIXTURE_MAP) == 0.0


def test_sum_cluster_cost_cross_track_and_theme_scoped():
    book_a = [_pos("NVDA", 10, 100.0), _pos("XOM", 5, 100.0)]   # discretionary
    book_b = [_pos("MRVL", 20, 50.0)]                            # paper-auto
    # AI cluster = NVDA(1000) + MRVL(1000) = 2000; XOM excluded (Energy).
    assert sum_cluster_cost("AI-momentum", [book_a, book_b], FIXTURE_MAP) == 2000.0
    assert sum_cluster_cost("Energy", [book_a, book_b], FIXTURE_MAP) == 500.0


def test_compute_breach_and_within():
    # account 100k, existing 25k AI, proposing 6k -> 31% > 30% -> breach
    e = compute(
        theme="AI-momentum",
        proposed_cost_usd=6_000.0,
        account_value_usd=100_000.0,
        existing_same_cluster_cost_usd=25_000.0,
    )
    assert e.output["breach"] is True
    assert e.output["binding_constraint"] == "theme_cluster_cap"
    assert math.isclose(e.output["cluster_pct"], 0.31, rel_tol=1e-9)

    # proposing 4k -> 29% <= 30% -> within
    e2 = compute(
        theme="AI-momentum",
        proposed_cost_usd=4_000.0,
        account_value_usd=100_000.0,
        existing_same_cluster_cost_usd=25_000.0,
    )
    assert e2.output["breach"] is False
    assert e2.output["binding_constraint"] == "within_cluster_cap"


def test_compute_untagged_is_clean_pass():
    e = compute(
        theme=None,
        proposed_cost_usd=50_000.0,   # huge, but no cluster
        account_value_usd=100_000.0,
        existing_same_cluster_cost_usd=0.0,
    )
    assert e.output["breach"] is False
    assert e.output["binding_constraint"] == "no_cluster"


def test_compute_rejects_bad_account():
    with pytest.raises(ValueError, match="account_value_usd"):
        compute(theme="AI-momentum", proposed_cost_usd=1.0,
                account_value_usd=0.0, existing_same_cluster_cost_usd=0.0)


def test_three_names_three_sectors_each_under_sector_cap_breach_cluster():
    """The blind spot this cap closes: three AI names in three different
    sectors, each within the 20% per-sector cap, together breach the 30%
    theme cap. account = 100k.
    """
    # NVDA (semis) 12%, CEG (power) 11%, DLR (DC REIT) 10% = 33% AI — each is a
    # different sector so the 20% sector cap never trips, but the cluster does.
    book = [_pos("NVDA", 120, 100.0), _pos("CEG", 110, 100.0)]  # 12k + 11k held
    e = compute_from_books(
        ticker="DLR",
        proposed_cost_usd=10_000.0,           # proposing the 3rd AI name
        account_value_usd=100_000.0,
        books=[book],
        theme_map=FIXTURE_MAP,
    )
    assert e.output["theme"] == "AI-momentum"
    assert math.isclose(e.output["cluster_total_usd"], 33_000.0, rel_tol=1e-9)
    assert e.output["breach"] is True


def test_compute_from_books_untagged_ticker_skips():
    book = [_pos("NVDA", 200, 100.0)]  # 20k AI already
    e = compute_from_books(
        ticker="WCC",                  # not in the map
        proposed_cost_usd=50_000.0,
        account_value_usd=100_000.0,
        books=[book],
        theme_map=FIXTURE_MAP,
    )
    assert e.output["binding_constraint"] == "no_cluster"
    assert e.output["breach"] is False


def test_real_map_loads_with_expected_membership():
    """The shipped curated map: AI complex present, non-AI holdings absent."""
    m = load_theme_map()
    assert m.get("NVDA") == "AI-momentum"
    assert m.get("MRVL") == "AI-momentum"
    assert m.get("CEG") == "AI-momentum"      # power, broader than one sector
    assert m.get("DLR") == "AI-momentum"      # DC REIT, broader than one sector
    # current non-AI discretionary holdings must NOT be tagged AI
    for t in ("WCC", "PLD", "TRGP", "VRDN"):
        assert m.get(t) is None


def test_default_cap_is_thirty_percent():
    assert DEFAULT_CLUSTER_CAP_PCT == 0.30
