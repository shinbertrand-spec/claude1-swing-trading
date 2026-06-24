"""Tests for tools.cluster_concentration — the theme/cluster correlation cap."""
from __future__ import annotations

import math

import pytest

from tools.cluster_concentration import (
    DEFAULT_CLUSTER_CAP_PCT,
    REGIME_CLUSTER_CAP,
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


# ----------------------------------------------------- regime-conditional cap


def _at(*, cluster_pct, regime_class, track, account=100_000.0):
    """Run compute() with existing=0 so proposed == cluster_total, giving a
    cluster of exactly ``cluster_pct``. Returns the output dict."""
    e = compute(
        theme="AI-momentum",
        proposed_cost_usd=cluster_pct * account,
        account_value_usd=account,
        existing_same_cluster_cost_usd=0.0,
        regime_class=regime_class,
        track=track,
    )
    return e.output


def test_effective_cap_scales_by_regime():
    """The effective cap is 0.30 / 0.25 / 0.20 / 0.15 by SPY stage."""
    expected = {
        "stage_2_confirmed": 0.30,
        "stage_2_weakening": 0.25,
        "stage_3_transitional": 0.20,
        "stage_4": 0.15,
    }
    assert REGIME_CLUSTER_CAP == expected
    for regime, cap in expected.items():
        out = _at(cluster_pct=0.10, regime_class=regime, track="discretionary")
        assert out["effective_cap_pct"] == cap
    # A 27% cluster: within the healthy-tape cap, breaches once the tape weakens.
    assert _at(cluster_pct=0.27, regime_class="stage_2_confirmed", track="discretionary")["breach"] is False
    assert _at(cluster_pct=0.27, regime_class="stage_2_weakening", track="discretionary")["breach"] is True
    assert _at(cluster_pct=0.27, regime_class="stage_3_transitional", track="discretionary")["breach"] is True
    assert _at(cluster_pct=0.27, regime_class="stage_4", track="discretionary")["breach"] is True


@pytest.mark.parametrize(
    "track, regime_class, cluster_pct, expected_action",
    [
        # no breach (5% cluster) -> always allow, regardless of track/regime
        ("paper-auto",    "stage_4",              0.05, "allow"),
        ("discretionary", "stage_2_weakening",    0.05, "allow"),
        (None,            None,                   0.05, "allow"),
        # breach (35% cluster, over every cap) -> action by (track, regime)
        ("paper-auto",    "stage_2_confirmed",    0.35, "block"),      # automated: hard, every regime
        ("paper-auto",    "stage_2_weakening",    0.35, "block"),
        ("paper-auto",    "stage_3_transitional", 0.35, "block"),
        ("paper-auto",    "stage_4",              0.35, "block"),
        ("discretionary", "stage_2_confirmed",    0.35, "warn"),       # healthy tape -> warn
        ("discretionary", "stage_2_weakening",    0.35, "half_size"),  # softening -> half
        ("discretionary", "stage_3_transitional", 0.35, "block"),      # risk-off -> block
        ("discretionary", "stage_4",              0.35, "block"),
        ("discretionary", None,                   0.35, "warn"),       # no regime -> flat WARN
        (None,            None,                   0.35, "warn"),       # unspecified -> WARN
    ],
)
def test_action_matrix(track, regime_class, cluster_pct, expected_action):
    out = _at(cluster_pct=cluster_pct, regime_class=regime_class, track=track)
    assert out["action"] == expected_action


def test_automated_track_hard_blocks_on_breach_in_every_regime():
    """The automated track stays a HARD block on breach in EVERY regime — incl.
    a cluster that only breaches because the regime tightened the cap."""
    # 22% cluster: under 0.30 (confirmed) but over 0.20 (stage_3) and 0.15 (stage_4).
    for regime in ("stage_2_confirmed", "stage_2_weakening", "stage_3_transitional", "stage_4"):
        out = _at(cluster_pct=0.22, regime_class=regime, track="paper-auto")
        if out["breach"]:
            assert out["action"] == "block", regime
    # half_size NEVER appears on the automated track, even in the weakening tape
    # where the discretionary track would half-size.
    out = _at(cluster_pct=0.35, regime_class="stage_2_weakening", track="paper-auto")
    assert out["action"] == "block"


def test_backward_compat_regime_none_reproduces_today():
    """regime_class=None (and track=None) reproduces the pre-regime behaviour
    exactly: cap measured at the flat 0.30, effective_cap == cap_pct."""
    # Same scenario as test_compute_breach_and_within: 31% vs flat 30% -> breach.
    e = compute(
        theme="AI-momentum",
        proposed_cost_usd=6_000.0,
        account_value_usd=100_000.0,
        existing_same_cluster_cost_usd=25_000.0,
    )
    assert e.output["breach"] is True
    assert e.output["binding_constraint"] == "theme_cluster_cap"
    assert e.output["cap_pct"] == 0.30
    assert e.output["effective_cap_pct"] == 0.30   # None regime -> no scaling
    assert e.output["regime_class"] is None
    # within case unchanged too
    e2 = compute(
        theme="AI-momentum",
        proposed_cost_usd=4_000.0,
        account_value_usd=100_000.0,
        existing_same_cluster_cost_usd=25_000.0,
    )
    assert e2.output["breach"] is False
    assert e2.output["effective_cap_pct"] == 0.30


def test_hard_ceiling_blocks_both_tracks_every_regime():
    """A cluster over the 0.45 hard ceiling blocks regardless of track/regime —
    incl. the discretionary track in a HEALTHY tape (where it would otherwise be
    a mere warning). This is the bet-the-whole-book backstop."""
    from tools.cluster_concentration import CLUSTER_HARD_CEILING
    assert CLUSTER_HARD_CEILING == 0.45
    for track in ("discretionary", "paper-auto", None):
        for regime in ("stage_2_confirmed", "stage_2_weakening",
                       "stage_3_transitional", "stage_4", None):
            out = _at(cluster_pct=0.50, regime_class=regime, track=track)
            assert out["action"] == "block", (track, regime)
            assert out["ceiling_breach"] is True
            assert out["binding_constraint"] == "theme_cluster_hard_ceiling"


def test_below_ceiling_healthy_tape_still_warns():
    """Just under the ceiling, a healthy-tape discretionary breach stays a WARN
    — the operator's concentration band (cap..ceiling) is preserved."""
    out = _at(cluster_pct=0.44, regime_class="stage_2_confirmed", track="discretionary")
    assert out["breach"] is True
    assert out["ceiling_breach"] is False
    assert out["action"] == "warn"


def test_cli_calibrate_writes_record(tmp_path, monkeypatch):
    """`main() --calibrate` appends a discretionary-cli record; the book path is
    a nonexistent file so the run is hermetic (existing cluster = 0)."""
    import json
    import sys

    from tools import cluster_concentration as cc
    monkeypatch.setenv("CLUSTER_CALIB_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", [
        "prog", "--ticker", "NVDA", "--proposed-cost", "9000",
        "--account", "100000", "--regime-class", "stage_2_weakening",
        "--track", "discretionary", "--calibrate",
        "--book", str(tmp_path / "nobook.json"),
    ])
    cc.main()
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(ln) for ln in files[0].read_text().splitlines() if ln.strip()]
    assert len(rows) == 1
    assert rows[0]["source"] == "discretionary-cli"
    assert rows[0]["ticker"] == "NVDA"
    assert rows[0]["track"] == "discretionary"
    assert rows[0]["regime_class"] == "stage_2_weakening"


def test_cli_without_calibrate_writes_nothing(tmp_path, monkeypatch):
    import sys

    from tools import cluster_concentration as cc
    monkeypatch.setenv("CLUSTER_CALIB_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", [
        "prog", "--ticker", "NVDA", "--proposed-cost", "9000",
        "--account", "100000", "--track", "discretionary",
        "--book", str(tmp_path / "nobook.json"),
    ])
    cc.main()
    assert list(tmp_path.glob("*.jsonl")) == []


def test_untagged_ticker_clean_pass_carries_regime_fields():
    """The theme=None early-return also emits the new fields (action=allow)."""
    e = compute(
        theme=None,
        proposed_cost_usd=50_000.0,
        account_value_usd=100_000.0,
        existing_same_cluster_cost_usd=0.0,
        regime_class="stage_4",
        track="discretionary",
    )
    assert e.output["breach"] is False
    assert e.output["action"] == "allow"
    assert e.output["effective_cap_pct"] == 0.15  # scaled, but no cluster to breach
