"""Tests for tools.thematic_portfolio.orchestrator."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import pytest

from tools.thematic_portfolio.orchestrator import (
    AggregatedPositionDecision,
    FilingPaths,
    PortfolioState,
    aggregate_critic_outputs,
    apply_aggregation_to_positions,
    build_live_bundle,
    compose_loop1_input_bundle,
    find_prior_loop1_output,
)


def _crit(critic: str, adj: str) -> dict:
    return {
        "critic": critic,
        "position_ticker": "X",
        "confidence_adjustment": adj,
        "risks": [],
        "adjustment_rationale": f"{critic} returned {adj}",
    }


# ---------------------------------------------------------------------------
# compose_loop1_input_bundle
# ---------------------------------------------------------------------------


def _sample_portfolio_state() -> PortfolioState:
    return PortfolioState(
        thematic_allocation_pct=10.0,
        current_loop5_phase="phase1_10pct",
        total_portfolio_nav_usd=1_000_000.0,
    )


def _sample_sa_lp_filing() -> FilingPaths:
    return FilingPaths(
        latest_period="2026-03-31",
        latest_long_book_path="ledgers/thematic/13f/sa_lp/0002045724-2026-03-31-long.json",
        latest_filed_date="2026-05-18",
        latest_put_complex_path="ledgers/thematic/13f/sa_lp/0002045724-2026-03-31-puts.json",
        latest_call_book_path="ledgers/thematic/13f/sa_lp/0002045724-2026-03-31-calls.json",
        prior_period="2025-12-31",
        prior_long_book_path="ledgers/thematic/13f/sa_lp/0002045724-2025-12-31-long.json",
    )


def _sample_ensemble_filings() -> dict[str, FilingPaths]:
    return {
        "altimeter": FilingPaths(
            latest_period="2026-03-31",
            latest_long_book_path="ledgers/thematic/13f/altimeter/latest-long.json",
        ),
        "coatue": FilingPaths(
            latest_period="2026-03-31",
            latest_long_book_path="ledgers/thematic/13f/coatue/latest-long.json",
        ),
        "light_street": FilingPaths(
            latest_period="2026-03-31",
            latest_long_book_path="ledgers/thematic/13f/light_street/latest-long.json",
        ),
    }


def _sample_corpus_snapshot() -> dict:
    return {
        "snapshot_id": "abc123",
        "refreshed_at": "2026-05-25T12:00:00Z",
        "paths": {"aschenbrenner_essays": "aschenbrenner/essays/*.md"},
        "slot_counts": {"aschenbrenner_essays": 9},
        "n_total_artifacts": 9,
    }


def test_bundle_monthly_base_trigger_shape():
    bundle = compose_loop1_input_bundle(
        trigger_type="monthly_base",
        fired_at="2026-06-01T09:30:00-04:00",
        triggering_artifact=None,
        rate_limit_consumed_this_week_before_firing=0,
        mandatory_escalation=False,
        corpus_snapshot=_sample_corpus_snapshot(),
        sa_lp_filing=_sample_sa_lp_filing(),
        ensemble_filings=_sample_ensemble_filings(),
        portfolio_state=_sample_portfolio_state(),
        prior_loop1_path=None,
    )
    assert bundle["trigger"]["type"] == "monthly_base"
    assert bundle["trigger"]["triggering_artifact"] is None
    assert bundle["prior_loop1_output"]["path"] is None
    assert "sa_lp" in bundle["filings"]
    assert set(bundle["filings"]["ensemble"].keys()) == {
        "altimeter",
        "coatue",
        "light_street",
    }
    assert bundle["portfolio_state"]["current_loop5_phase"] == "phase1_10pct"


def test_bundle_substantive_artifact_trigger_requires_artifact():
    with pytest.raises(ValueError, match="requires triggering_artifact"):
        compose_loop1_input_bundle(
            trigger_type="substantive_artifact",
            fired_at="2026-06-01T09:30:00-04:00",
            triggering_artifact=None,
            rate_limit_consumed_this_week_before_firing=0,
            mandatory_escalation=False,
            corpus_snapshot=_sample_corpus_snapshot(),
            sa_lp_filing=_sample_sa_lp_filing(),
            ensemble_filings=_sample_ensemble_filings(),
            portfolio_state=_sample_portfolio_state(),
            prior_loop1_path=None,
        )


def test_bundle_monthly_base_rejects_triggering_artifact():
    """monthly_base trigger must NOT carry an artifact (the spec is explicit)."""
    artifact = {"source": "13f:sa_lp", "url": "...", "tier": 1, "snippet": "..."}
    with pytest.raises(ValueError, match="must NOT carry"):
        compose_loop1_input_bundle(
            trigger_type="monthly_base",
            fired_at="2026-06-01T09:30:00-04:00",
            triggering_artifact=artifact,
            rate_limit_consumed_this_week_before_firing=0,
            mandatory_escalation=False,
            corpus_snapshot=_sample_corpus_snapshot(),
            sa_lp_filing=_sample_sa_lp_filing(),
            ensemble_filings=_sample_ensemble_filings(),
            portfolio_state=_sample_portfolio_state(),
            prior_loop1_path=None,
        )


def test_bundle_invalid_trigger_type():
    with pytest.raises(ValueError, match="trigger_type must be one of"):
        compose_loop1_input_bundle(
            trigger_type="weekly_extra",  # invalid
            fired_at="2026-06-01T09:30:00-04:00",
            triggering_artifact=None,
            rate_limit_consumed_this_week_before_firing=0,
            mandatory_escalation=False,
            corpus_snapshot=_sample_corpus_snapshot(),
            sa_lp_filing=_sample_sa_lp_filing(),
            ensemble_filings=_sample_ensemble_filings(),
            portfolio_state=_sample_portfolio_state(),
            prior_loop1_path=None,
        )


def test_bundle_unknown_ensemble_fund_rejected():
    bad_ensemble = {
        "altimeter": _sample_ensemble_filings()["altimeter"],
        "druckenmiller": FilingPaths("2026-03-31", "path.json"),
    }
    with pytest.raises(ValueError, match="unknown ensemble fund"):
        compose_loop1_input_bundle(
            trigger_type="monthly_base",
            fired_at="2026-06-01T09:30:00-04:00",
            triggering_artifact=None,
            rate_limit_consumed_this_week_before_firing=0,
            mandatory_escalation=False,
            corpus_snapshot=_sample_corpus_snapshot(),
            sa_lp_filing=_sample_sa_lp_filing(),
            ensemble_filings=bad_ensemble,
            portfolio_state=_sample_portfolio_state(),
            prior_loop1_path=None,
        )


def test_bundle_rate_limit_count_bounds():
    """rate_limit_consumed_this_week must be in [0, 3]."""
    for bad in (-1, 4, 100):
        with pytest.raises(ValueError, match=r"rate_limit_consumed"):
            compose_loop1_input_bundle(
                trigger_type="monthly_base",
                fired_at="2026-06-01T09:30:00-04:00",
                triggering_artifact=None,
                rate_limit_consumed_this_week_before_firing=bad,
                mandatory_escalation=False,
                corpus_snapshot=_sample_corpus_snapshot(),
                sa_lp_filing=_sample_sa_lp_filing(),
                ensemble_filings=_sample_ensemble_filings(),
                portfolio_state=_sample_portfolio_state(),
                prior_loop1_path=None,
            )


def test_bundle_with_artifact_trigger_shape():
    artifact = {
        "source": "x:@leopoldasch",
        "url": "https://x.com/leopoldasch/status/1",
        "tier": 2,
        "snippet": "SNDK update post...",
    }
    bundle = compose_loop1_input_bundle(
        trigger_type="substantive_artifact",
        fired_at="2026-06-15T14:30:00-04:00",
        triggering_artifact=artifact,
        rate_limit_consumed_this_week_before_firing=1,
        mandatory_escalation=False,
        corpus_snapshot=_sample_corpus_snapshot(),
        sa_lp_filing=_sample_sa_lp_filing(),
        ensemble_filings=_sample_ensemble_filings(),
        portfolio_state=_sample_portfolio_state(),
        prior_loop1_path="ledgers/thematic/loop1/2026-06-01T0930.json",
    )
    assert bundle["trigger"]["triggering_artifact"]["source"] == "x:@leopoldasch"
    assert bundle["trigger"]["rate_limit_consumed_this_week"] == 1
    assert bundle["prior_loop1_output"]["path"] == "ledgers/thematic/loop1/2026-06-01T0930.json"


def test_bundle_round_trip_is_json_serializable():
    bundle = compose_loop1_input_bundle(
        trigger_type="monthly_base",
        fired_at="2026-06-01T09:30:00-04:00",
        triggering_artifact=None,
        rate_limit_consumed_this_week_before_firing=0,
        mandatory_escalation=False,
        corpus_snapshot=_sample_corpus_snapshot(),
        sa_lp_filing=_sample_sa_lp_filing(),
        ensemble_filings=_sample_ensemble_filings(),
        portfolio_state=_sample_portfolio_state(),
        prior_loop1_path=None,
    )
    json.dumps(bundle)


# ---------------------------------------------------------------------------
# PortfolioState validation
# ---------------------------------------------------------------------------


def test_portfolio_state_rejects_bad_allocation():
    with pytest.raises(ValueError, match="thematic_allocation_pct"):
        PortfolioState(
            thematic_allocation_pct=20.0,  # not 10/15/25
            current_loop5_phase="phase1_10pct",
            total_portfolio_nav_usd=1_000_000.0,
        )


def test_portfolio_state_rejects_bad_phase():
    with pytest.raises(ValueError, match="current_loop5_phase"):
        PortfolioState(
            thematic_allocation_pct=10.0,
            current_loop5_phase="phase4_30pct",
            total_portfolio_nav_usd=1_000_000.0,
        )


def test_portfolio_state_rejects_nonpositive_nav():
    with pytest.raises(ValueError, match="total_portfolio_nav_usd"):
        PortfolioState(
            thematic_allocation_pct=10.0,
            current_loop5_phase="phase1_10pct",
            total_portfolio_nav_usd=0.0,
        )


# ---------------------------------------------------------------------------
# find_prior_loop1_output
# ---------------------------------------------------------------------------


def test_find_prior_loop1_returns_none_when_dir_missing(tmp_path: Path):
    assert find_prior_loop1_output(tmp_path / "doesnt-exist") is None


def test_find_prior_loop1_returns_none_when_dir_empty(tmp_path: Path):
    assert find_prior_loop1_output(tmp_path) is None


def test_find_prior_loop1_picks_most_recent(tmp_path: Path):
    old = tmp_path / "2026-05-01T0930.json"
    old.write_text("{}")
    time.sleep(0.01)  # ensure mtime ordering
    new = tmp_path / "2026-06-01T0930.json"
    new.write_text("{}")
    result = find_prior_loop1_output(tmp_path)
    assert result is not None
    assert Path(result).name == "2026-06-01T0930.json"


def test_find_prior_loop1_skips_underscore_prefixed(tmp_path: Path):
    """Files like _state/ subdirs or _index.json are skipped."""
    (tmp_path / "_index.json").write_text("{}")
    (tmp_path / "2026-06-01T0930.json").write_text("{}")
    result = find_prior_loop1_output(tmp_path)
    assert Path(result).name == "2026-06-01T0930.json"


def test_find_prior_loop1_skips_double_underscore_sidecars(tmp_path: Path):
    """Sidecar files (__bundle, __aggregated, __critic_outputs) are skipped —
    only the canonical ``<fired_at>.json`` output counts as a prior firing."""
    canonical = tmp_path / "2026-05-25T0749.json"
    canonical.write_text("{}")
    time.sleep(0.01)
    # These sidecars have newer mtime but must not be picked.
    (tmp_path / "2026-05-25T0749__bundle.json").write_text("{}")
    (tmp_path / "2026-05-25T0749__aggregated.json").write_text("[]")
    result = find_prior_loop1_output(tmp_path)
    assert Path(result).name == "2026-05-25T0749.json"


# ---------------------------------------------------------------------------
# build_live_bundle
# ---------------------------------------------------------------------------


def _stub_manifest(corpus_root, since):  # noqa: ARG001
    return {
        "snapshot_id": "stub",
        "refreshed_at": "2026-05-25T07:49:25Z",
        "paths": {"aschenbrenner_essays": "aschenbrenner/essays/*.md"},
        "slot_counts": {"aschenbrenner_essays": 1},
        "n_total_artifacts": 1,
        "since": since,
    }


def test_build_live_bundle_first_ever_firing(tmp_path: Path):
    """No state.json, no prior Loop 1 — defaults compose a phase1_10pct bundle."""
    bundle = build_live_bundle(
        trigger_type="monthly_base",
        fired_at="2026-05-25T07:49:25+00:00",
        sa_lp_period="2026-03-31",
        prior_sa_lp_period="2025-12-31",
        sa_lp_filed_date="2026-05-18",
        ensemble_filed_dates={"altimeter": "2026-05-15", "coatue": "2026-05-15", "light_street": "2026-05-15"},
        loop1_dir=tmp_path / "loop1",
        state_file=tmp_path / "doesnt-exist.json",
        thirteen_f_root=tmp_path / "13f",
        corpus_root=tmp_path / "corpus",
        nav_provider=lambda: 1_000_000.0,
        compose_manifest_fn=_stub_manifest,
    )

    assert bundle["trigger"]["type"] == "monthly_base"
    assert bundle["trigger"]["fired_at"] == "2026-05-25T07:49:25+00:00"
    assert bundle["portfolio_state"]["thematic_allocation_pct"] == 10.0
    assert bundle["portfolio_state"]["current_loop5_phase"] == "phase1_10pct"
    assert bundle["portfolio_state"]["total_portfolio_nav_usd"] == 1_000_000.0
    assert bundle["portfolio_state"]["current_thematic_positions"] == []
    assert bundle["prior_loop1_output"]["path"] is None
    assert bundle["corpus_snapshot"]["snapshot_id"] == "stub"
    assert bundle["corpus_snapshot"]["since"] == "1970-01-01T00:00:00Z"

    sa_lp = bundle["filings"]["sa_lp"]
    assert sa_lp["latest_13f"]["period"] == "2026-03-31"
    assert sa_lp["latest_13f"]["filed"] == "2026-05-18"
    assert Path(sa_lp["latest_13f"]["long_book_path"]).name == "0002045724-2026-03-31-long.json"
    assert Path(sa_lp["latest_13f"]["long_book_path"]).parent.name == "sa_lp"
    assert sa_lp["prior_13f"]["period"] == "2025-12-31"

    ensemble = bundle["filings"]["ensemble"]
    assert set(ensemble.keys()) == {"altimeter", "coatue", "light_street"}
    coatue_path = Path(ensemble["coatue"]["latest_13f"]["long_book_path"])
    assert coatue_path.name == "0001135730-2026-03-31-long.json"
    assert coatue_path.parent.name == "coatue"


def test_build_live_bundle_reads_state_file(tmp_path: Path):
    """Existing state.json drives allocation + phase + held positions."""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "thematic_allocation_pct": 15.0,
        "current_loop5_phase": "phase2_15pct",
        "current_thematic_positions": [
            {"ticker": "BE", "shares": 100, "cost_basis": 25.0, "current_weight_pct_of_total": 2.3},
        ],
    }))

    bundle = build_live_bundle(
        trigger_type="monthly_base",
        fired_at="2026-09-01T13:30:00+00:00",
        sa_lp_period="2026-06-30",
        prior_sa_lp_period="2026-03-31",
        loop1_dir=tmp_path / "loop1",
        state_file=state_file,
        thirteen_f_root=tmp_path / "13f",
        corpus_root=tmp_path / "corpus",
        nav_provider=lambda: 1_250_000.0,
        compose_manifest_fn=_stub_manifest,
    )

    assert bundle["portfolio_state"]["thematic_allocation_pct"] == 15.0
    assert bundle["portfolio_state"]["current_loop5_phase"] == "phase2_15pct"
    assert bundle["portfolio_state"]["total_portfolio_nav_usd"] == 1_250_000.0
    assert len(bundle["portfolio_state"]["current_thematic_positions"]) == 1


def test_build_live_bundle_allocation_override(tmp_path: Path):
    """``allocation_pct_override`` wins over both state.json and the default."""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "thematic_allocation_pct": 10.0,
        "current_loop5_phase": "phase1_10pct",
        "current_thematic_positions": [],
    }))

    bundle = build_live_bundle(
        trigger_type="monthly_base",
        fired_at="2026-05-25T07:49:25+00:00",
        sa_lp_period="2026-03-31",
        prior_sa_lp_period=None,
        loop1_dir=tmp_path / "loop1",
        state_file=state_file,
        thirteen_f_root=tmp_path / "13f",
        corpus_root=tmp_path / "corpus",
        allocation_pct_override=25.0,
        nav_provider=lambda: 1_000_000.0,
        compose_manifest_fn=_stub_manifest,
    )

    assert bundle["portfolio_state"]["thematic_allocation_pct"] == 25.0
    # When prior_sa_lp_period is None, the bundle either omits prior_13f
    # entirely or sets it to None — either is acceptable.
    assert bundle["filings"]["sa_lp"].get("prior_13f") is None


def test_build_live_bundle_uses_prior_loop1_fired_at_as_since(tmp_path: Path):
    """When a prior Loop 1 exists, ``since`` in the corpus manifest call should
    be the prior firing's ``meta.fired_at`` (used for recent-artifact filtering)."""
    loop1_dir = tmp_path / "loop1"
    loop1_dir.mkdir()
    prior_path = loop1_dir / "2026-04-01T1330.json"
    prior_path.write_text(json.dumps({
        "meta": {"fired_at": "2026-04-01T13:30:00+00:00"},
    }))

    seen_since: dict[str, str] = {}

    def capture_manifest(corpus_root, since):  # noqa: ARG001
        seen_since["value"] = since
        return _stub_manifest(corpus_root, since)

    bundle = build_live_bundle(
        trigger_type="monthly_base",
        fired_at="2026-05-25T07:49:25+00:00",
        sa_lp_period="2026-03-31",
        prior_sa_lp_period="2025-12-31",
        loop1_dir=loop1_dir,
        state_file=tmp_path / "doesnt-exist.json",
        thirteen_f_root=tmp_path / "13f",
        corpus_root=tmp_path / "corpus",
        nav_provider=lambda: 1_000_000.0,
        compose_manifest_fn=capture_manifest,
    )

    assert seen_since["value"] == "2026-04-01T13:30:00+00:00"
    assert bundle["prior_loop1_output"]["path"] == str(prior_path)


# ---------------------------------------------------------------------------
# aggregate_critic_outputs
# ---------------------------------------------------------------------------


def test_aggregate_single_structural_risk_holds():
    decision = aggregate_critic_outputs(
        ticker="NVDA",
        loop1_target_pct=3.5,
        critic_outputs=[
            _crit("thorstad", "structural_risk"),
            _crit("marcus", "hold"),
            _crit("lecun", "minus_20"),
        ],
    )
    assert decision.recommended_action == "hold_pending_bertrand_review"
    assert decision.adjusted_target_pct == 3.5  # unchanged
    assert decision.weight_reduction_applied == 0.0
    assert decision.structural_risk_critics == ["thorstad"]


def test_aggregate_single_minus_50_holds():
    decision = aggregate_critic_outputs(
        ticker="SNDK",
        loop1_target_pct=4.7,
        critic_outputs=[
            _crit("patel", "minus_50"),
            _crit("rasgon", "minus_20"),
            _crit("marcus", "hold"),
        ],
    )
    assert decision.recommended_action == "hold_pending_bertrand_review"
    assert decision.adjusted_target_pct == 4.7  # unchanged
    assert decision.minus_50_critics == ["patel"]


def test_aggregate_two_minus_20_triggers_weighted_reduction():
    decision = aggregate_critic_outputs(
        ticker="NVDA",
        loop1_target_pct=3.0,
        critic_outputs=[
            _crit("marcus", "minus_20"),
            _crit("lecun", "minus_20"),
            _crit("thorstad", "hold"),
            _crit("friedman_extended", "hold"),
            _crit("mechanize_epoch", "hold"),
        ],
    )
    assert decision.recommended_action == "trim"
    # avg of two minus_20s = 0.20; new target = 3.0 × 0.80 = 2.4
    assert math.isclose(decision.adjusted_target_pct, 2.4, rel_tol=1e-9)
    assert decision.n_critics_minus_20 == 2


def test_aggregate_single_minus_20_preserves_target():
    decision = aggregate_critic_outputs(
        ticker="VST",
        loop1_target_pct=2.5,
        critic_outputs=[
            _crit("marcus", "minus_20"),
            _crit("lecun", "hold"),
            _crit("thorstad", "hold"),
            _crit("friedman_extended", "hold"),
            _crit("mechanize_epoch", "hold"),
        ],
    )
    assert decision.recommended_action == "preserve"
    assert decision.adjusted_target_pct == 2.5
    assert decision.weight_reduction_applied == 0.0
    assert "marcus" in decision.rationale


def test_aggregate_all_holds_preserves_target():
    decision = aggregate_critic_outputs(
        ticker="CEG",
        loop1_target_pct=4.0,
        critic_outputs=[
            _crit("marcus", "hold"),
            _crit("lecun", "hold"),
            _crit("thorstad", "hold"),
            _crit("friedman_extended", "hold"),
            _crit("mechanize_epoch", "hold"),
        ],
    )
    assert decision.recommended_action == "preserve"
    assert decision.adjusted_target_pct == 4.0


def test_aggregate_structural_risk_priority_over_minus_50():
    """If both structural_risk AND minus_50 fire, structural_risk wins rationale."""
    decision = aggregate_critic_outputs(
        ticker="NVDA",
        loop1_target_pct=3.0,
        critic_outputs=[
            _crit("thorstad", "structural_risk"),
            _crit("patel", "minus_50"),
        ],
    )
    assert decision.recommended_action == "hold_pending_bertrand_review"
    assert "STRUCTURAL_RISK" in decision.rationale


def test_aggregate_rejects_invalid_confidence_adjustment():
    with pytest.raises(ValueError, match="invalid confidence_adjustment"):
        aggregate_critic_outputs(
            ticker="X",
            loop1_target_pct=1.0,
            critic_outputs=[_crit("marcus", "minus_99")],
        )


def test_aggregate_rejects_missing_keys():
    with pytest.raises(ValueError, match="missing"):
        aggregate_critic_outputs(
            ticker="X",
            loop1_target_pct=1.0,
            critic_outputs=[{"position_ticker": "X"}],  # no critic / no adj
        )


# ---------------------------------------------------------------------------
# apply_aggregation_to_positions
# ---------------------------------------------------------------------------


def test_apply_aggregation_walks_all_positions():
    loop1_positions = [
        {"ticker": "NVDA", "target_weight_pct_of_total": 3.0},
        {"ticker": "SNDK", "target_weight_pct_of_total": 4.7},
        {"ticker": "BE", "target_weight_pct_of_total": 5.0},
    ]
    critic_outputs = {
        "NVDA": [_crit("thorstad", "structural_risk")],
        "SNDK": [_crit("patel", "minus_20"), _crit("rasgon", "minus_20")],
        "BE": [_crit("marcus", "hold"), _crit("friedman_extended", "hold")],
    }
    decisions = apply_aggregation_to_positions(loop1_positions, critic_outputs)
    assert len(decisions) == 3
    by_ticker = {d.ticker: d for d in decisions}
    assert by_ticker["NVDA"].recommended_action == "hold_pending_bertrand_review"
    assert by_ticker["SNDK"].recommended_action == "trim"
    assert math.isclose(by_ticker["SNDK"].adjusted_target_pct, 4.7 * 0.8, rel_tol=1e-9)
    assert by_ticker["BE"].recommended_action == "preserve"


def test_apply_aggregation_handles_position_with_no_critic_outputs():
    loop1_positions = [{"ticker": "ORPHAN", "target_weight_pct_of_total": 1.5}]
    decisions = apply_aggregation_to_positions(loop1_positions, {})
    assert decisions[0].recommended_action == "preserve"
    assert decisions[0].adjusted_target_pct == 1.5
    assert "No critic outputs" in decisions[0].rationale
