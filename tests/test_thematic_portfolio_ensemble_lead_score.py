"""Tests for tools.thematic_portfolio.ensemble_lead_score (Loop 6 Pass 3)."""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from tools.thematic_portfolio import Position
from tools.thematic_portfolio.ensemble_lead_score import (
    BUCKET_SCORES,
    ENSEMBLE_ADD_WEIGHT,
    ENSEMBLE_FUNDS,
    EnsembleLeadCandidate,
    EnsembleLeadResult,
    MIN_HOLDER_COUNT,
    N_HOLDERS_WEIGHT,
    SECTOR_BUCKET_AI_CHIP_MAKERS,
    SECTOR_BUCKET_AI_POWER_INFRA,
    SECTOR_BUCKET_HYPERSCALERS,
    SECTOR_BUCKET_OTHER,
    SECTOR_CLASSIFICATION_MAP,
    THESIS_ALIGNMENT_WEIGHT,
    classify_sector,
    compute_ensemble_lead_score,
    compute_from_paths,
)


def _book(*items: tuple[str, float]) -> list[Position]:
    return [Position(ticker=t, issuer_name=f"{t} INC", value_usd=v) for t, v in items]


def _ensemble(altimeter, coatue, light_street):
    return {"altimeter": altimeter, "coatue": coatue, "light_street": light_street}


def _run(sa_lp, latest, prior=None) -> EnsembleLeadResult:
    return compute_ensemble_lead_score(
        sa_lp_book=sa_lp,
        ensemble_books_latest=latest,
        ensemble_books_prior=prior,
        period_latest="2026-03-31",
        period_prior="2025-12-31" if prior is not None else None,
    )


# ---------------------------------------------------------------------------
# Sector classification
# ---------------------------------------------------------------------------


def test_classify_sector_known_tickers():
    assert classify_sector("BE") == SECTOR_BUCKET_AI_POWER_INFRA
    assert classify_sector("CEG") == SECTOR_BUCKET_AI_POWER_INFRA
    assert classify_sector("NVDA") == SECTOR_BUCKET_AI_CHIP_MAKERS
    assert classify_sector("AMD") == SECTOR_BUCKET_AI_CHIP_MAKERS
    assert classify_sector("MSFT") == SECTOR_BUCKET_HYPERSCALERS
    assert classify_sector("AMZN") == SECTOR_BUCKET_HYPERSCALERS


def test_classify_sector_unknown_defaults_other():
    """Unknown ticker defaults to OTHER (score 0.1)."""
    assert classify_sector("DEFINITELYNOTATICKER") == SECTOR_BUCKET_OTHER
    assert classify_sector("CHYM") == SECTOR_BUCKET_OTHER  # Chime — fintech, off-thesis


def test_bucket_scores_locked():
    """v1-locked per gate-3 decision 2.3 — change requires ≥ 4 calibration cycles."""
    assert BUCKET_SCORES[SECTOR_BUCKET_AI_POWER_INFRA] == 1.0
    assert BUCKET_SCORES[SECTOR_BUCKET_AI_CHIP_MAKERS] == 0.8
    assert BUCKET_SCORES[SECTOR_BUCKET_HYPERSCALERS] == 0.6
    assert BUCKET_SCORES[SECTOR_BUCKET_OTHER] == 0.1


def test_weight_coefficients_locked():
    """0.4 / 0.4 / 0.2 — v1-locked per gate-3 decision 2.3."""
    assert N_HOLDERS_WEIGHT == 0.4
    assert THESIS_ALIGNMENT_WEIGHT == 0.4
    assert ENSEMBLE_ADD_WEIGHT == 0.2
    assert MIN_HOLDER_COUNT == 2


# ---------------------------------------------------------------------------
# Candidate-set construction
# ---------------------------------------------------------------------------


def test_sa_lp_holdings_excluded_from_candidates():
    """A ticker held by SA LP can never be a candidate, regardless of ensemble overlap."""
    sa_lp = _book(("NVDA", 100.0))
    latest = _ensemble(
        _book(("NVDA", 100.0)),
        _book(("NVDA", 100.0)),
        _book(("NVDA", 100.0)),
    )
    result = _run(sa_lp, latest)
    assert result.n_candidates == 0


def test_single_holder_excluded():
    """A ticker held by exactly 1 ensemble fund is below the MIN_HOLDER_COUNT floor."""
    sa_lp = _book()
    latest = _ensemble(
        _book(("X", 100.0)),
        _book(),
        _book(),
    )
    result = _run(sa_lp, latest)
    assert result.n_candidates == 0


def test_two_holder_included():
    """Two-holder candidates pass the floor."""
    sa_lp = _book()
    latest = _ensemble(
        _book(("X", 100.0)),
        _book(("X", 100.0)),
        _book(),
    )
    result = _run(sa_lp, latest)
    assert result.n_candidates == 1
    assert result.candidates[0].ticker == "X"
    assert result.candidates[0].ensemble_holders == ["altimeter", "coatue"]


def test_three_holder_max_score_contribution():
    """3-fund consensus: 3 * 0.4 = 1.2 on n_holders_term."""
    sa_lp = _book()
    latest = _ensemble(
        _book(("X", 100.0)),
        _book(("X", 100.0)),
        _book(("X", 100.0)),
    )
    result = _run(sa_lp, latest)
    cand = result.candidates[0]
    assert cand.components.n_ensemble_holders == 3
    assert math.isclose(cand.components.n_holders_term, 1.2)


# ---------------------------------------------------------------------------
# Scoring math
# ---------------------------------------------------------------------------


def test_score_formula_full_breakdown():
    """Verify the locked formula end-to-end on a known input.

    AVGO held by all 3 ensemble funds (3 * 0.4 = 1.2),
    classified as AI-chip-makers (0.8 * 0.4 = 0.32),
    not newly added (0 * 0.2 = 0.0).
    Total: 1.2 + 0.32 + 0.0 = 1.52
    """
    sa_lp = _book()
    latest = _ensemble(
        _book(("AVGO", 100.0)),
        _book(("AVGO", 100.0)),
        _book(("AVGO", 100.0)),
    )
    prior = _ensemble(
        _book(("AVGO", 90.0)),
        _book(("AVGO", 90.0)),
        _book(("AVGO", 90.0)),
    )
    result = _run(sa_lp, latest, prior)
    cand = result.candidates[0]
    assert cand.ticker == "AVGO"
    assert math.isclose(cand.components.n_holders_term, 1.2)
    assert math.isclose(cand.components.thesis_alignment_term, 0.32)
    assert cand.components.ensemble_added_this_quarter is False
    assert math.isclose(cand.components.ensemble_added_term, 0.0)
    assert math.isclose(cand.total_score, 1.52)


def test_score_formula_with_newly_added():
    """Newly-added bumps the score by +0.2."""
    sa_lp = _book()
    latest = _ensemble(
        _book(("BE", 100.0)),
        _book(("BE", 100.0)),
        _book(),
    )
    prior = _ensemble(
        _book(),                  # altimeter newly added
        _book(("BE", 90.0)),      # coatue held it before
        _book(),
    )
    result = _run(sa_lp, latest, prior)
    cand = result.candidates[0]
    # 2 holders * 0.4 = 0.8 + 1.0 * 0.4 (AI-power-infra) + 0.2 (newly added)
    assert cand.components.n_ensemble_holders == 2
    assert cand.components.thesis_alignment_bucket == SECTOR_BUCKET_AI_POWER_INFRA
    assert cand.components.ensemble_added_this_quarter is True
    assert cand.newly_added_by == ["altimeter"]
    assert math.isclose(cand.total_score, 0.8 + 0.4 + 0.2)


def test_unknown_ticker_scored_as_other():
    """A ticker not in the classification map gets 0.1 alignment."""
    sa_lp = _book()
    latest = _ensemble(
        _book(("CHYM", 100.0)),
        _book(("CHYM", 100.0)),
        _book(),
    )
    result = _run(sa_lp, latest)
    cand = result.candidates[0]
    assert cand.components.thesis_alignment_bucket == SECTOR_BUCKET_OTHER
    assert math.isclose(cand.components.thesis_alignment_score, 0.1)
    # 2 * 0.4 + 0.1 * 0.4 + 0 = 0.84
    assert math.isclose(cand.total_score, 0.84)


# ---------------------------------------------------------------------------
# ensemble_added behavior across edge cases
# ---------------------------------------------------------------------------


def test_no_prior_period_no_added_signal():
    """When prior books are absent, ensemble_added is always False (no signal)."""
    sa_lp = _book()
    latest = _ensemble(
        _book(("MSFT", 100.0)),
        _book(("MSFT", 100.0)),
        _book(),
    )
    result = _run(sa_lp, latest, prior=None)
    cand = result.candidates[0]
    assert cand.components.ensemble_added_this_quarter is False
    assert cand.components.ensemble_added_term == 0.0
    assert cand.newly_added_by == []


def test_partial_prior_period_raises():
    """ensemble_books_prior must have all 3 funds or be None."""
    sa_lp = _book()
    latest = _ensemble(_book(), _book(), _book())
    partial_prior = {"altimeter": _book(), "coatue": _book()}  # missing light_street
    with pytest.raises(ValueError, match="missing funds"):
        compute_ensemble_lead_score(
            sa_lp_book=sa_lp,
            ensemble_books_latest=latest,
            ensemble_books_prior=partial_prior,  # type: ignore[arg-type]
            period_latest="2026-03-31",
            period_prior="2025-12-31",
        )


def test_multiple_funds_newly_added():
    """When 2 funds both newly add, newly_added_by lists both."""
    sa_lp = _book()
    latest = _ensemble(
        _book(("NEWNAME", 100.0)),
        _book(("NEWNAME", 100.0)),
        _book(),
    )
    prior = _ensemble(_book(), _book(), _book())  # neither held it
    result = _run(sa_lp, latest, prior)
    cand = result.candidates[0]
    assert cand.newly_added_by == ["altimeter", "coatue"]
    # ensemble_added_term is binary 0.2 not 0.4 — it's a "any fund added" flag, not a per-fund count
    assert math.isclose(cand.components.ensemble_added_term, 0.2)


# ---------------------------------------------------------------------------
# Ranking + determinism
# ---------------------------------------------------------------------------


def test_candidates_sorted_by_score_desc():
    """Higher score ranks first."""
    sa_lp = _book()
    latest = _ensemble(
        _book(("BE", 100.0), ("CHYM", 100.0)),
        _book(("BE", 100.0), ("CHYM", 100.0), ("NVDA", 100.0)),
        _book(("NVDA", 100.0)),
    )
    result = _run(sa_lp, latest)
    scores = [c.total_score for c in result.candidates]
    assert scores == sorted(scores, reverse=True)


def test_tie_broken_alphabetically():
    """When scores tie, sort by ticker ascending."""
    sa_lp = _book()
    # Two unknown tickers with identical signals → identical scores → ticker tiebreak
    latest = _ensemble(
        _book(("ZETA", 100.0), ("ALPHA", 100.0)),
        _book(("ZETA", 100.0), ("ALPHA", 100.0)),
        _book(),
    )
    result = _run(sa_lp, latest)
    assert [c.ticker for c in result.candidates] == ["ALPHA", "ZETA"]


def test_holders_list_deterministic_sort():
    """ensemble_holders is sorted; iteration order from the input doesn't leak."""
    sa_lp = _book()
    latest = _ensemble(
        _book(("X", 100.0)),
        _book(("X", 100.0)),
        _book(("X", 100.0)),
    )
    result = _run(sa_lp, latest)
    assert result.candidates[0].ensemble_holders == ["altimeter", "coatue", "light_street"]


# ---------------------------------------------------------------------------
# to_dict + JSON serialization
# ---------------------------------------------------------------------------


def test_to_dict_round_trips_via_json():
    sa_lp = _book(("NVDA", 100.0))
    latest = _ensemble(
        _book(("BE", 100.0)),
        _book(("BE", 100.0)),
        _book(),
    )
    result = _run(sa_lp, latest)
    d = result.to_dict()
    restored = json.loads(json.dumps(d))
    assert restored["n_candidates"] == 1
    assert restored["candidates"][0]["ticker"] == "BE"
    assert restored["sa_lp_universe_size"] == 1


def test_result_universe_sizes():
    sa_lp = _book(("S1", 1.0), ("S2", 1.0), ("S3", 1.0))
    latest = _ensemble(
        _book(("A1", 1.0), ("A2", 1.0)),
        _book(("C1", 1.0)),
        _book(("L1", 1.0), ("L2", 1.0), ("L3", 1.0), ("L4", 1.0)),
    )
    result = _run(sa_lp, latest)
    assert result.sa_lp_universe_size == 3
    assert result.ensemble_universe_sizes == {
        "altimeter": 2, "coatue": 1, "light_street": 4,
    }


# ---------------------------------------------------------------------------
# compute_from_paths integration
# ---------------------------------------------------------------------------


def test_compute_from_paths_partial_prior_raises(tmp_path: Path):
    """File-path wrapper enforces all-or-none prior."""
    sa_lp_path = tmp_path / "sa.json"
    a_path = tmp_path / "a.json"
    c_path = tmp_path / "c.json"
    ls_path = tmp_path / "ls.json"
    a_prior = tmp_path / "a_prior.json"
    for p in [sa_lp_path, a_path, c_path, ls_path, a_prior]:
        p.write_text("[]")
    with pytest.raises(ValueError, match="ALL three ensemble prior paths"):
        compute_from_paths(
            sa_lp_path=sa_lp_path,
            altimeter_path=a_path,
            coatue_path=c_path,
            light_street_path=ls_path,
            altimeter_prior_path=a_prior,
            coatue_prior_path=None,
            light_street_prior_path=None,
            period_latest="2026-03-31",
        )


def test_compute_from_paths_smoke(tmp_path: Path):
    """End-to-end on synthetic JSON files."""
    def w(path: Path, rows: list[dict]) -> Path:
        path.write_text(json.dumps(rows))
        return path

    sa_lp_path    = w(tmp_path / "sa.json",  [{"ticker": "NVDA", "issuer_name": "NVIDIA", "value_usd": 100.0}])
    a_path        = w(tmp_path / "a.json",   [{"ticker": "MSFT", "issuer_name": "MICROSOFT", "value_usd": 100.0}])
    c_path        = w(tmp_path / "c.json",   [{"ticker": "MSFT", "issuer_name": "MICROSOFT", "value_usd": 100.0}])
    ls_path       = w(tmp_path / "ls.json",  [{"ticker": "BE",   "issuer_name": "BLOOM ENERGY", "value_usd": 100.0}])
    trace = compute_from_paths(
        sa_lp_path=sa_lp_path,
        altimeter_path=a_path,
        coatue_path=c_path,
        light_street_path=ls_path,
        period_latest="2026-03-31",
    )
    assert trace.tool == "tools/thematic_portfolio/ensemble_lead_score.py"
    out = trace.output
    assert out["sa_lp_universe_size"] == 1
    # MSFT held by 2 (altimeter+coatue), not in SA LP → candidate
    # BE held by 1 (light_street only) → not a candidate
    assert out["n_candidates"] == 1
    assert out["candidates"][0]["ticker"] == "MSFT"


# ---------------------------------------------------------------------------
# Real-data smoke (Q4 2025 → Q1 2026)
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_real_thirteen_f_smoke():
    """End-to-end on real Q1 2026 + Q4 2025 13F data."""
    paths = {
        "sa_lp": REPO_ROOT / "ledgers/thematic/13f/sa_lp/0002045724-2026-03-31-long.json",
        "altimeter": REPO_ROOT / "ledgers/thematic/13f/altimeter/0001541617-2026-03-31-long.json",
        "coatue": REPO_ROOT / "ledgers/thematic/13f/coatue/0001135730-2026-03-31-long.json",
        "light_street": REPO_ROOT / "ledgers/thematic/13f/light_street/0001569049-2026-03-31-long.json",
        "altimeter_prior": REPO_ROOT / "ledgers/thematic/13f/altimeter/0001541617-2025-12-31-long.json",
        "coatue_prior": REPO_ROOT / "ledgers/thematic/13f/coatue/0001135730-2025-12-31-long.json",
        "light_street_prior": REPO_ROOT / "ledgers/thematic/13f/light_street/0001569049-2025-12-31-long.json",
    }
    if not all(p.exists() for p in paths.values()):
        pytest.skip("13F data not on disk for all 4 funds")

    trace = compute_from_paths(
        sa_lp_path=paths["sa_lp"],
        altimeter_path=paths["altimeter"],
        coatue_path=paths["coatue"],
        light_street_path=paths["light_street"],
        altimeter_prior_path=paths["altimeter_prior"],
        coatue_prior_path=paths["coatue_prior"],
        light_street_prior_path=paths["light_street_prior"],
        period_latest="2026-03-31",
        period_prior="2025-12-31",
    )
    out = trace.output
    # Q1 2026 known candidate set per the live probe: 6 names
    assert out["n_candidates"] == 6
    tickers = {c["ticker"] for c in out["candidates"]}
    assert tickers == {"AMZN", "AVGO", "CHYM", "GOOGL", "META", "MSFT"}
    # All scores within [0, 2.0] envelope (max possible: 3*0.4 + 1.0*0.4 + 0.2 = 1.8)
    for c in out["candidates"]:
        assert 0.0 <= c["total_score"] <= 2.0
    # All known tickers (AMZN, AVGO, etc.) classified, not OTHER
    sectors = {c["ticker"]: c["components"]["thesis_alignment_bucket"] for c in out["candidates"]}
    assert sectors["AVGO"] == SECTOR_BUCKET_AI_CHIP_MAKERS
    assert sectors["MSFT"] == SECTOR_BUCKET_HYPERSCALERS
    assert sectors["AMZN"] == SECTOR_BUCKET_HYPERSCALERS
    assert sectors["GOOGL"] == SECTOR_BUCKET_HYPERSCALERS
    assert sectors["META"] == SECTOR_BUCKET_HYPERSCALERS
    # CHYM (Chime) is fintech / off-thesis — should fall through to OTHER
    assert sectors["CHYM"] == SECTOR_BUCKET_OTHER


def test_cli_runs_against_real_data():
    """Smoke-test the python -m entry point against real data."""
    paths = {
        "sa_lp": REPO_ROOT / "ledgers/thematic/13f/sa_lp/0002045724-2026-03-31-long.json",
        "altimeter": REPO_ROOT / "ledgers/thematic/13f/altimeter/0001541617-2026-03-31-long.json",
        "coatue": REPO_ROOT / "ledgers/thematic/13f/coatue/0001135730-2026-03-31-long.json",
        "light_street": REPO_ROOT / "ledgers/thematic/13f/light_street/0001569049-2026-03-31-long.json",
    }
    if not all(p.exists() for p in paths.values()):
        pytest.skip("13F data not on disk")
    result = subprocess.run(
        [
            sys.executable, "-m", "tools.thematic_portfolio.ensemble_lead_score",
            "--sa-lp", str(paths["sa_lp"]),
            "--altimeter", str(paths["altimeter"]),
            "--coatue", str(paths["coatue"]),
            "--light-street", str(paths["light_street"]),
            "--period-latest", "2026-03-31",
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["tool"] == "tools/thematic_portfolio/ensemble_lead_score.py"
    assert payload["output"]["n_candidates"] == 6
