"""Tests for tools.thematic_portfolio.tier3.ai_capex_announcements."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.thematic_portfolio.tier3.ai_capex_announcements import (
    CAPEX_XBRL_CONCEPT,
    HYPERSCALERS,
    SCHEMA_VERSION,
    _classify_trend,
    _snapshot_from_capex_dict,
    compose,
)


# ---------------------------------------------------------------------------
# Catalog discipline
# ---------------------------------------------------------------------------


def test_hyperscalers_locked_set_v1():
    """The 5-hyperscaler set is the demand-side anchor for SA LP's power
    thesis. Changing it is a design decision, not a maintenance edit."""
    assert set(HYPERSCALERS.keys()) == {"MSFT", "META", "GOOGL", "AMZN", "ORCL"}


def test_hyperscalers_company_names_populated():
    for ticker, name in HYPERSCALERS.items():
        assert name, f"company name missing for {ticker}"
        assert len(name) > 3


def test_capex_xbrl_concept_is_canonical():
    """The XBRL concept used MUST be PaymentsToAcquirePropertyPlantAndEquipment.
    Switching to a different concept (e.g., PurchasesOfPropertyPlantAndEquipment)
    is a substantive change because not all issuers report under both names."""
    assert CAPEX_XBRL_CONCEPT == "PaymentsToAcquirePropertyPlantAndEquipment"


def test_capex_fallback_chain_includes_productive_assets_for_amzn():
    """AMZN switched from the canonical concept to
    PaymentsToAcquireProductiveAssets in 2018+; the fallback chain MUST
    include it so the AMZN fetch succeeds end-to-end. Verified live
    against EDGAR 2026-05-26 (AMZN FY 2025 capex $131.8B via the
    fallback concept)."""
    from tools.thematic_portfolio.tier3.ai_capex_announcements import (
        CAPEX_XBRL_FALLBACK_CONCEPTS,
    )
    assert "PaymentsToAcquireProductiveAssets" in CAPEX_XBRL_FALLBACK_CONCEPTS


# ---------------------------------------------------------------------------
# _classify_trend
# ---------------------------------------------------------------------------


def test_classify_trend_accelerating_at_threshold():
    assert _classify_trend(20.0) == "accelerating"
    assert _classify_trend(30.0) == "accelerating"
    assert _classify_trend(80.0) == "accelerating"


def test_classify_trend_decelerating_at_threshold():
    assert _classify_trend(-10.0) == "decelerating"
    assert _classify_trend(-25.0) == "decelerating"


def test_classify_trend_flat_in_between():
    assert _classify_trend(0.0) == "flat"
    assert _classify_trend(5.0) == "flat"
    assert _classify_trend(19.9) == "flat"
    assert _classify_trend(-9.9) == "flat"


def test_classify_trend_none_propagates_none():
    assert _classify_trend(None) is None


# ---------------------------------------------------------------------------
# _snapshot_from_capex_dict
# ---------------------------------------------------------------------------


def test_snapshot_happy_path_msft_2024_2025():
    capex = {"FY 2024": 44_477_000_000.0, "FY 2025": 88_242_000_000.0}
    s = _snapshot_from_capex_dict("MSFT", "Microsoft Corporation", capex)
    assert s.ticker == "MSFT"
    assert s.latest_fy == "FY 2025"
    assert s.latest_fy_capex_usd == 88_242_000_000.0
    assert s.prior_fy == "FY 2024"
    assert s.prior_fy_capex_usd == 44_477_000_000.0
    assert s.yoy_change_pct == pytest.approx(98.39, rel=1e-2)
    assert s.trend == "accelerating"
    assert s.fetch_status == "ok"


def test_snapshot_handles_single_fy_only():
    """When only one FY column is returned, prior_fy + yoy are None but
    fetch_status stays ok (the latest year is still real data)."""
    s = _snapshot_from_capex_dict("X", "X Corp", {"FY 2025": 10_000_000_000.0})
    assert s.latest_fy == "FY 2025"
    assert s.latest_fy_capex_usd == 10_000_000_000.0
    assert s.prior_fy is None
    assert s.yoy_change_pct is None
    assert s.trend is None
    assert s.fetch_status == "ok"


def test_snapshot_sorts_fy_chronologically_even_if_dict_unordered():
    """edgartools sometimes returns FY columns in non-sorted order; we
    must sort by year and pick the highest as `latest`."""
    capex = {"FY 2023": 30.0, "FY 2025": 50.0, "FY 2024": 40.0}
    s = _snapshot_from_capex_dict("X", "X", capex)
    assert s.latest_fy == "FY 2025"
    assert s.latest_fy_capex_usd == 50.0
    assert s.prior_fy == "FY 2024"


def test_snapshot_empty_input_becomes_error_snapshot():
    s = _snapshot_from_capex_dict("X", "X", {})
    assert s.fetch_status == "error"
    assert s.error_reason == "no_fy_columns_returned"
    assert s.latest_fy is None


def test_snapshot_zero_prior_year_avoids_division():
    """Some issuers report 0 capex in a prior year (e.g., reorg). YoY
    becomes None rather than blowing up with ZeroDivisionError."""
    capex = {"FY 2024": 0.0, "FY 2025": 1_000_000_000.0}
    s = _snapshot_from_capex_dict("X", "X", capex)
    assert s.yoy_change_pct is None
    assert s.fetch_status == "ok"


def test_snapshot_negative_yoy_classifies_decelerating():
    capex = {"FY 2024": 100.0, "FY 2025": 80.0}
    s = _snapshot_from_capex_dict("X", "X", capex)
    assert s.yoy_change_pct == pytest.approx(-20.0)
    assert s.trend == "decelerating"


# ---------------------------------------------------------------------------
# compose — full payload shape
# ---------------------------------------------------------------------------


def _stub_fetcher(responses: dict[str, dict[str, float]] | None = None, raise_on: set[str] | None = None):
    """Build a per-ticker capex-fetcher stub. Missing tickers get a
    default 20%-growth scenario; ``raise_on`` simulates EDGAR failures."""
    responses = responses or {}
    raise_on = raise_on or set()

    def _fn(ticker: str) -> dict[str, float]:
        if ticker in raise_on:
            raise RuntimeError(f"simulated EDGAR failure for {ticker}")
        return responses.get(ticker, {"FY 2024": 50_000_000_000.0, "FY 2025": 60_000_000_000.0})

    return _fn


def test_compose_returns_well_formed_payload():
    trace = compose(
        capex_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    p = trace.output
    assert p["schema_version"] == SCHEMA_VERSION
    assert p["fetched_at"] == "2026-05-26T10:00:00+00:00"
    assert p["xbrl_concept"] == CAPEX_XBRL_CONCEPT
    assert len(p["hyperscalers"]) == 5
    assert p["aggregate"]["n_attempted"] == 5
    assert p["aggregate"]["n_ok"] == 5
    assert p["errors"] == []


def test_compose_aggregate_thesis_signal_accelerating():
    """When >=3 hyperscalers are in accelerating bucket, aggregate flags
    accelerating."""
    responses = {
        "MSFT":  {"FY 2024": 40e9, "FY 2025": 88e9},   # +120%
        "META":  {"FY 2024": 28e9, "FY 2025": 50e9},   # +78%
        "GOOGL": {"FY 2024": 32e9, "FY 2025": 58e9},   # +81%
        "AMZN":  {"FY 2024": 49e9, "FY 2025": 80e9},   # +63%
        "ORCL":  {"FY 2024":  7e9, "FY 2025": 25e9},   # +257%
    }
    trace = compose(
        capex_fetcher=_stub_fetcher(responses=responses),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert trace.output["aggregate"]["thesis_signal"] == "accelerating"
    assert trace.output["aggregate"]["n_accelerating"] == 5
    assert trace.output["aggregate"]["median_yoy_change_pct"] > 50


def test_compose_aggregate_thesis_signal_decelerating():
    """Inverse case — all 5 cut capex. Decelerating verdict."""
    responses = {t: {"FY 2024": 50e9, "FY 2025": 40e9} for t in HYPERSCALERS}  # -20%
    trace = compose(
        capex_fetcher=_stub_fetcher(responses=responses),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert trace.output["aggregate"]["thesis_signal"] == "decelerating"
    assert trace.output["aggregate"]["n_decelerating"] == 5


def test_compose_aggregate_thesis_signal_mixed_when_split():
    """3+ neither accelerating nor decelerating = mixed."""
    responses = {
        "MSFT":  {"FY 2024": 50e9, "FY 2025": 100e9},  # +100% acc
        "META":  {"FY 2024": 50e9, "FY 2025":  52e9},  # +4% flat
        "GOOGL": {"FY 2024": 50e9, "FY 2025":  51e9},  # +2% flat
        "AMZN":  {"FY 2024": 50e9, "FY 2025":  53e9},  # +6% flat
        "ORCL":  {"FY 2024": 50e9, "FY 2025":  40e9},  # -20% decel
    }
    trace = compose(
        capex_fetcher=_stub_fetcher(responses=responses),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert trace.output["aggregate"]["thesis_signal"] == "mixed"


def test_compose_per_ticker_failure_does_not_abort():
    """EDGAR failures on one ticker MUST NOT abort the rest."""
    trace = compose(
        capex_fetcher=_stub_fetcher(raise_on={"ORCL", "META"}),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    p = trace.output
    assert p["aggregate"]["n_ok"] == 3
    assert {e["ticker"] for e in p["errors"]} == {"ORCL", "META"}
    rows = {r["ticker"]: r for r in p["hyperscalers"]}
    assert rows["ORCL"]["fetch_status"] == "error"
    assert rows["ORCL"]["error_reason"].startswith("RuntimeError")
    assert rows["ORCL"]["latest_fy"] is None


def test_compose_writes_json_when_out_path_set(tmp_path: Path):
    out = tmp_path / "subdir" / "ai_capex.json"
    trace = compose(
        out_path=out,
        capex_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert out.exists()
    on_disk = json.loads(out.read_text())
    assert on_disk == trace.output
    assert on_disk["schema_version"] == SCHEMA_VERSION


def test_compose_does_not_write_when_out_path_none(tmp_path: Path):
    trace = compose(
        out_path=None,
        capex_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert list(tmp_path.iterdir()) == []
    assert trace.output["aggregate"]["n_ok"] > 0


def test_compose_accepts_hyperscalers_override():
    mini = {"MSFT": "Microsoft Corporation"}
    trace = compose(
        hyperscalers=mini,
        capex_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert trace.output["aggregate"]["n_attempted"] == 1
    assert {r["ticker"] for r in trace.output["hyperscalers"]} == {"MSFT"}


def test_compose_no_data_aggregate_signal():
    """When every ticker errors, thesis_signal is 'no_data' not crash."""
    trace = compose(
        capex_fetcher=_stub_fetcher(raise_on=set(HYPERSCALERS.keys())),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert trace.output["aggregate"]["thesis_signal"] == "no_data"
    assert trace.output["aggregate"]["n_ok"] == 0


def test_compose_emits_traceentry():
    trace = compose(
        capex_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert trace.tool == "tools/thematic_portfolio/tier3/ai_capex_announcements.py"
    assert trace.inputs["n_hyperscalers"] == 5


def test_compose_orchestrator_discovery_compatible():
    """The output filename must match the orchestrator's _TIER3_SLOT_FILES
    expectation so build_live_bundle auto-includes it."""
    from tools.thematic_portfolio.orchestrator import _TIER3_SLOT_FILES
    assert "ai_capex_announcements" in _TIER3_SLOT_FILES
    assert _TIER3_SLOT_FILES["ai_capex_announcements"] == "ai_capex_announcements.json"
