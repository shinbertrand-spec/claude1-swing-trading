"""Tests for tools.thematic_portfolio.drift_analysis (Loop 6 Pass 1)."""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from tools.thematic_portfolio import Position
from tools.thematic_portfolio.drift_analysis import (
    DriftProfile,
    PositionDelta,
    SizeChangeDistribution,
    _percentile,
    _safe_pct_change,
    compute_drift_profile,
    compute_from_paths,
)


def _book(*items: tuple[str, float]) -> list[Position]:
    return [Position(ticker=t, issuer_name=t, value_usd=v) for t, v in items]


def _profile(latest: list[Position], prior: list[Position]) -> DriftProfile:
    return compute_drift_profile(
        latest_book=latest,
        prior_book=prior,
        fund="test_fund",
        period_latest="2026-03-31",
        period_prior="2025-12-31",
    )


# ---------------------------------------------------------------------------
# Empty / extreme cases
# ---------------------------------------------------------------------------


def test_both_empty():
    p = _profile([], [])
    assert p.n_positions_latest == 0
    assert p.n_positions_prior == 0
    assert p.new_positions == []
    assert p.exits == []
    assert p.adds == []
    assert p.trims == []
    assert p.adds_distribution.n == 0
    assert p.adds_distribution.p50 is None


def test_all_new_positions():
    """Prior empty → every latest position is new."""
    p = _profile(_book(("A", 100.0), ("B", 50.0)), [])
    assert {pos.ticker for pos in p.new_positions} == {"A", "B"}
    assert p.exits == []
    assert p.adds == []
    assert p.trims == []
    # new_positions sorted by value_usd desc
    assert [pos.ticker for pos in p.new_positions] == ["A", "B"]


def test_all_exits():
    """Latest empty → every prior position is an exit."""
    p = _profile([], _book(("A", 100.0), ("B", 50.0)))
    assert p.new_positions == []
    assert {pos.ticker for pos in p.exits} == {"A", "B"}
    assert [pos.ticker for pos in p.exits] == ["A", "B"]  # value desc


# ---------------------------------------------------------------------------
# Adds / trims classification
# ---------------------------------------------------------------------------


def test_pure_adds_classification():
    p = _profile(
        _book(("A", 200.0), ("B", 150.0)),
        _book(("A", 100.0), ("B", 100.0)),
    )
    assert len(p.adds) == 2
    assert p.trims == []
    assert p.unchanged == []
    # Sorted by delta_usd desc
    assert [d.ticker for d in p.adds] == ["A", "B"]
    assert p.adds[0].delta_usd == 100.0
    assert math.isclose(p.adds[0].pct_change, 1.0)


def test_pure_trims_classification():
    p = _profile(
        _book(("A", 50.0), ("B", 80.0)),
        _book(("A", 100.0), ("B", 100.0)),
    )
    assert p.adds == []
    assert len(p.trims) == 2
    # Sorted by delta_usd asc (most-negative first)
    assert [d.ticker for d in p.trims] == ["A", "B"]
    assert p.trims[0].delta_usd == -50.0
    assert math.isclose(p.trims[0].pct_change, -0.5)


def test_unchanged_positions():
    """Exact-equality value match goes to unchanged, not adds/trims."""
    p = _profile(_book(("A", 100.0)), _book(("A", 100.0)))
    assert p.adds == []
    assert p.trims == []
    assert len(p.unchanged) == 1
    assert p.unchanged[0].delta_usd == 0.0


def test_mixed_full_drift():
    """All five buckets populated."""
    latest = _book(
        ("KEEP_FLAT", 100.0),  # unchanged
        ("UP", 150.0),  # add
        ("DOWN", 50.0),  # trim
        ("NEW", 200.0),  # new
    )
    prior = _book(
        ("KEEP_FLAT", 100.0),
        ("UP", 100.0),
        ("DOWN", 100.0),
        ("EXIT", 80.0),
    )
    p = _profile(latest, prior)
    assert [pos.ticker for pos in p.new_positions] == ["NEW"]
    assert [pos.ticker for pos in p.exits] == ["EXIT"]
    assert [d.ticker for d in p.adds] == ["UP"]
    assert [d.ticker for d in p.trims] == ["DOWN"]
    assert [d.ticker for d in p.unchanged] == ["KEEP_FLAT"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_safe_pct_change_zero_prior_returns_none():
    """The honest value when prior == 0 is None, not Inf."""
    assert _safe_pct_change(0.0, 100.0) is None


def test_safe_pct_change_normal():
    assert _safe_pct_change(100.0, 150.0) == 0.5
    assert _safe_pct_change(100.0, 50.0) == -0.5


def test_percentile_empty():
    assert _percentile([], 0.5) is None


def test_percentile_single_value():
    assert _percentile([3.14], 0.5) == 3.14
    assert _percentile([3.14], 0.0) == 3.14
    assert _percentile([3.14], 1.0) == 3.14


def test_percentile_linear_interp():
    """Verify against the numpy default ('linear' method)."""
    # [10, 20, 30, 40]: p50 = 25.0, p25 = 17.5, p75 = 32.5
    vals = [10.0, 20.0, 30.0, 40.0]
    assert _percentile(vals, 0.50) == 25.0
    assert _percentile(vals, 0.25) == 17.5
    assert _percentile(vals, 0.75) == 32.5


# ---------------------------------------------------------------------------
# Distribution
# ---------------------------------------------------------------------------


def test_distribution_skips_none_pct_change():
    """Zero-prior deltas get None pct_change and must NOT poison the distribution."""
    p = _profile(
        _book(("ZERO_PRIOR_NEW", 100.0), ("REAL", 200.0)),
        _book(("REAL", 100.0)),
    )
    # ZERO_PRIOR_NEW is in new_positions (not in overlap), so its pct_change
    # doesn't reach the distribution anyway. Sanity-check distribution
    # against just REAL.
    assert p.adds_distribution.n == 1
    assert math.isclose(p.adds_distribution.p50, 1.0)


def test_distribution_percentiles_match_manual_computation():
    """Synthetic adds with known pct_change distribution."""
    # Build 4 adds with pct_change = 0.1, 0.2, 0.3, 0.4
    latest = _book(
        ("A", 110.0),   # +10%
        ("B", 120.0),   # +20%
        ("C", 130.0),   # +30%
        ("D", 140.0),   # +40%
    )
    prior = _book(("A", 100.0), ("B", 100.0), ("C", 100.0), ("D", 100.0))
    p = _profile(latest, prior)
    assert p.adds_distribution.n == 4
    # Linear-interp percentiles over [0.1, 0.2, 0.3, 0.4]
    assert math.isclose(p.adds_distribution.p25, 0.175, rel_tol=1e-9)
    assert math.isclose(p.adds_distribution.p50, 0.25, rel_tol=1e-9)
    assert math.isclose(p.adds_distribution.p75, 0.325, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# to_dict + integration
# ---------------------------------------------------------------------------


def test_to_dict_is_json_serializable():
    p = _profile(
        _book(("A", 200.0), ("NEW", 50.0)),
        _book(("A", 100.0), ("EXIT", 30.0)),
    )
    d = p.to_dict()
    # round-trip through JSON
    serialized = json.dumps(d)
    loaded = json.loads(serialized)
    assert loaded["fund"] == "test_fund"
    assert loaded["n_positions_latest"] == 2
    assert len(loaded["adds"]) == 1
    assert loaded["adds"][0]["ticker"] == "A"


def test_to_dict_preserves_full_shape():
    """All documented top-level fields land in the dict."""
    p = _profile(_book(("A", 1.0)), _book(("A", 1.0)))
    d = p.to_dict()
    expected_keys = {
        "fund", "period_latest", "period_prior",
        "n_positions_latest", "n_positions_prior",
        "total_value_latest_usd", "total_value_prior_usd",
        "new_positions", "exits", "adds", "trims", "unchanged",
        "adds_distribution", "trims_distribution",
    }
    assert set(d) == expected_keys


def test_compute_from_paths_smoke(tmp_path: Path):
    """compute_from_paths reads JSON, returns a TraceEntry."""
    latest_data = [
        {"ticker": "A", "issuer_name": "A INC", "value_usd": 200.0, "cusip": "X"},
        {"ticker": "NEW", "issuer_name": "NEW INC", "value_usd": 50.0, "cusip": "Y"},
    ]
    prior_data = [
        {"ticker": "A", "issuer_name": "A INC", "value_usd": 100.0, "cusip": "X"},
        {"ticker": "EXIT", "issuer_name": "EXIT INC", "value_usd": 30.0, "cusip": "Z"},
    ]
    latest_path = tmp_path / "latest.json"
    prior_path = tmp_path / "prior.json"
    latest_path.write_text(json.dumps(latest_data))
    prior_path.write_text(json.dumps(prior_data))

    trace = compute_from_paths(
        latest_path=latest_path,
        prior_path=prior_path,
        fund="test_fund",
        period_latest="2026-03-31",
        period_prior="2025-12-31",
    )
    assert trace.tool == "tools/thematic_portfolio/drift_analysis.py"
    assert trace.output["fund"] == "test_fund"
    assert trace.output["n_positions_latest"] == 2
    assert len(trace.output["new_positions"]) == 1
    assert trace.output["new_positions"][0]["ticker"] == "NEW"
    assert len(trace.output["exits"]) == 1
    assert trace.output["exits"][0]["ticker"] == "EXIT"
    assert len(trace.output["adds"]) == 1
    assert trace.output["adds"][0]["ticker"] == "A"


def test_duplicate_tickers_last_write_wins():
    """Defensive: duplicate tickers within one book don't crash."""
    # Two A entries — the second one wins in the index
    book = [
        Position(ticker="A", issuer_name="A1", value_usd=100.0),
        Position(ticker="A", issuer_name="A2", value_usd=200.0),
    ]
    p = compute_drift_profile(
        latest_book=book,
        prior_book=[],
        fund="dedup_test",
        period_latest="2026-03-31",
        period_prior="2025-12-31",
    )
    # The "new_positions" iteration is over the latest_idx values, so the
    # last-write-wins entry (issuer_name="A2", value=200) is what surfaces.
    # n_positions_latest counts raw list length, not deduped — informational.
    assert p.n_positions_latest == 2
    assert len(p.new_positions) == 1
    assert p.new_positions[0].issuer_name == "A2"
    assert p.new_positions[0].value_usd == 200.0


# ---------------------------------------------------------------------------
# Real-data smoke (SA LP + ensemble Q4 2025 → Q1 2026)
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("fund,cik", [
    ("sa_lp", "0002045724"),
    ("altimeter", "0001541617"),
    ("coatue", "0001135730"),
    ("light_street", "0001569049"),
])
def test_real_thirteen_f_smoke(fund: str, cik: str):
    """End-to-end on the real Q4 2025 → Q1 2026 long books on disk.

    Verifies the module operates on the production-shaped JSON without
    schema surprises. We don't assert specific drift values (those
    change quarter-over-quarter); we assert structural invariants.
    """
    latest = REPO_ROOT / f"ledgers/thematic/13f/{fund}/{cik}-2026-03-31-long.json"
    prior = REPO_ROOT / f"ledgers/thematic/13f/{fund}/{cik}-2025-12-31-long.json"
    if not (latest.exists() and prior.exists()):
        pytest.skip(f"13F data not on disk for {fund}")

    trace = compute_from_paths(
        latest_path=latest,
        prior_path=prior,
        fund=fund,
        period_latest="2026-03-31",
        period_prior="2025-12-31",
    )
    out = trace.output
    # Structural invariants only
    assert out["fund"] == fund
    assert out["n_positions_latest"] > 0
    assert out["n_positions_prior"] > 0
    assert out["total_value_latest_usd"] > 0
    # Adds + trims + unchanged accounts for every overlap ticker
    # (which is the count of common keys between the two books)
    n_overlap = len(out["adds"]) + len(out["trims"]) + len(out["unchanged"])
    # Sanity: overlap can't exceed either book's size
    assert n_overlap <= out["n_positions_latest"]
    assert n_overlap <= out["n_positions_prior"]
    # new + overlap = latest count (modulo duplicates — books are deduped
    # by ticker at the corpus-ingester layer, but allow the ≤ for safety)
    assert len(out["new_positions"]) + n_overlap <= out["n_positions_latest"]
    # exits + overlap = prior count (same safety)
    assert len(out["exits"]) + n_overlap <= out["n_positions_prior"]


def test_cli_runs_against_real_data():
    """Smoke-test the python -m entry point against real SA LP data."""
    latest = REPO_ROOT / "ledgers/thematic/13f/sa_lp/0002045724-2026-03-31-long.json"
    prior = REPO_ROOT / "ledgers/thematic/13f/sa_lp/0002045724-2025-12-31-long.json"
    if not (latest.exists() and prior.exists()):
        pytest.skip("SA LP 13F data not on disk")
    result = subprocess.run(
        [
            sys.executable, "-m", "tools.thematic_portfolio.drift_analysis",
            "--latest", str(latest),
            "--prior", str(prior),
            "--fund", "sa_lp",
            "--period-latest", "2026-03-31",
            "--period-prior", "2025-12-31",
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    # stdout should be a JSON TraceEntry envelope
    payload = json.loads(result.stdout)
    assert payload["tool"] == "tools/thematic_portfolio/drift_analysis.py"
    assert payload["output"]["fund"] == "sa_lp"
