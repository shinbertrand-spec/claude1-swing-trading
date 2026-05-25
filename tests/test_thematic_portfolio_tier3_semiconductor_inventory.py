"""Tests for tools.thematic_portfolio.tier3.semiconductor_inventory."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from tools.thematic_portfolio.tier3.semiconductor_inventory import (
    COGS_XBRL_CONCEPTS,
    DAYS_IN_QUARTER,
    INVENTORY_FETCH_TICKERS,
    INVENTORY_XBRL_CONCEPT,
    SCHEMA_VERSION,
    SEMI_TICKERS,
    _classify_thesis_signal,
    _inventory_days,
    _median,
    _qoq_pct,
    _snapshot_from_data,
    _top_level_row_value,
    compose,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _momentum(
    *,
    last_close=100.0,
    change_30d_pct=5.0,
    change_90d_pct=10.0,
    change_ytd_pct=15.0,
) -> dict:
    return {
        "last_close": last_close,
        "change_30d_pct": change_30d_pct,
        "change_90d_pct": change_90d_pct,
        "change_ytd_pct": change_ytd_pct,
    }


def _inventory(
    *,
    inventory_latest_usd=25_797_000_000.0,
    inventory_latest_period="2026-04-26",
    inventory_prior_usd=21_403_000_000.0,
    inventory_prior_period="2026-01-25",
    cogs_latest_usd=20_458_000_000.0,
) -> dict:
    return {
        "inventory_latest_usd": inventory_latest_usd,
        "inventory_latest_period": inventory_latest_period,
        "inventory_prior_usd": inventory_prior_usd,
        "inventory_prior_period": inventory_prior_period,
        "cogs_latest_usd": cogs_latest_usd,
    }


def _stub_momentum_fetcher(
    responses: dict[str, dict] | None = None,
    raise_on: set[str] | None = None,
):
    responses = responses or {}
    raise_on = raise_on or set()

    def _fn(symbol: str) -> dict:
        if symbol in raise_on:
            raise RuntimeError(f"simulated momentum failure for {symbol}")
        return responses.get(symbol, _momentum())

    return _fn


def _stub_inventory_fetcher(
    responses: dict[str, dict] | None = None,
    raise_on: set[str] | None = None,
):
    responses = responses or {}
    raise_on = raise_on or set()

    def _fn(ticker: str) -> dict:
        if ticker in raise_on:
            raise RuntimeError(f"simulated inventory failure for {ticker}")
        return responses.get(ticker, _inventory())

    return _fn


# ---------------------------------------------------------------------------
# Catalog discipline
# ---------------------------------------------------------------------------


def test_catalog_categories_are_the_four_v1_classes():
    assert set(SEMI_TICKERS.keys()) == {
        "semis_index",
        "foundry",
        "gpu",
        "memory_hbm",
    }


def test_catalog_includes_soxx_and_smh_in_semis_index():
    """SOXX + SMH are the two retail-accessible broad-tape semis ETFs and
    MUST be present — they drive the momentum half of the classifier."""
    semis = set(SEMI_TICKERS["semis_index"])
    assert "SOXX" in semis
    assert "SMH" in semis


def test_catalog_includes_nvda_and_amd_in_gpu():
    assert set(SEMI_TICKERS["gpu"]) == {"NVDA", "AMD"}


def test_inventory_fetch_subset_is_only_us_10q_filers():
    """TSM (foreign issuer, 6-K) and ETFs MUST NOT be in the inventory
    fetch set."""
    assert INVENTORY_FETCH_TICKERS == frozenset({"NVDA", "AMD", "MU"})
    for t in INVENTORY_FETCH_TICKERS:
        # Every inventory-fetch ticker must appear in the main catalog
        assert any(t in syms for syms in SEMI_TICKERS.values())


def test_catalog_symbols_are_uppercase_and_unique():
    seen: set[str] = set()
    for cat, syms in SEMI_TICKERS.items():
        for s in syms:
            assert s == s.upper()
            assert s not in seen, f"duplicate {s} across categories"
            seen.add(s)


def test_cogs_concept_priority_includes_all_known_variants():
    """NVDA uses CostOfRevenue; AMD + MU use CostOfGoodsAndServicesSold;
    older filings use CostOfGoodsSold. All three MUST be in the priority list."""
    assert "us-gaap_CostOfRevenue" in COGS_XBRL_CONCEPTS
    assert "us-gaap_CostOfGoodsAndServicesSold" in COGS_XBRL_CONCEPTS
    assert "us-gaap_CostOfGoodsSold" in COGS_XBRL_CONCEPTS


def test_inventory_xbrl_concept_pinned():
    assert INVENTORY_XBRL_CONCEPT == "us-gaap_InventoryNet"


# ---------------------------------------------------------------------------
# _qoq_pct + _inventory_days helpers
# ---------------------------------------------------------------------------


def test_qoq_pct_happy_path():
    # NVDA Q1 FY27: $25.80B vs prior $21.40B = +20.53%
    assert abs(_qoq_pct(25_797_000_000, 21_403_000_000) - 20.53) < 0.1


def test_qoq_pct_returns_none_on_missing():
    assert _qoq_pct(None, 100.0) is None
    assert _qoq_pct(100.0, None) is None
    assert _qoq_pct(100.0, 0.0) is None  # divide-by-zero guard


def test_inventory_days_happy_path():
    # NVDA Q1 FY27: $25.80B inv / $20.46B cogs × 91 = ~114.7
    days = _inventory_days(25_797_000_000, 20_458_000_000)
    assert days is not None
    assert abs(days - 114.7) < 1.0


def test_inventory_days_returns_none_on_missing():
    assert _inventory_days(None, 100.0) is None
    assert _inventory_days(100.0, None) is None
    assert _inventory_days(100.0, 0.0) is None


def test_days_in_quarter_constant_is_91():
    assert DAYS_IN_QUARTER == 91


# ---------------------------------------------------------------------------
# _top_level_row_value
# ---------------------------------------------------------------------------


def test_top_level_row_value_picks_non_segmented():
    """When the same concept appears multiple times (segmented breakouts),
    only the row with empty/null dimension counts as top-level."""
    df = pd.DataFrame({
        "concept": ["us-gaap_CostOfRevenue", "us-gaap_CostOfRevenue"],
        "dimension": ["", "Segment_A"],
        "2026-04-26": [20_458_000_000.0, 5_000_000_000.0],
    })
    val = _top_level_row_value(df, "us-gaap_CostOfRevenue", "2026-04-26")
    # MUST be the top-level value, not the segment one
    assert val == 20_458_000_000.0


def test_top_level_row_value_returns_none_when_missing():
    df = pd.DataFrame({
        "concept": ["us-gaap_OtherThing"],
        "dimension": [""],
        "2026-04-26": [100.0],
    })
    assert _top_level_row_value(df, "us-gaap_InventoryNet", "2026-04-26") is None


def test_top_level_row_value_returns_none_when_nan():
    df = pd.DataFrame({
        "concept": ["us-gaap_InventoryNet"],
        "dimension": [""],
        "2026-04-26": [float("nan")],
    })
    assert _top_level_row_value(df, "us-gaap_InventoryNet", "2026-04-26") is None


def test_top_level_row_value_handles_em_dash_dimension():
    """edgartools sometimes uses em-dash for empty dimension."""
    df = pd.DataFrame({
        "concept": ["us-gaap_InventoryNet"],
        "dimension": ["—"],
        "2026-04-26": [25_797_000_000.0],
    })
    assert _top_level_row_value(df, "us-gaap_InventoryNet", "2026-04-26") == 25_797_000_000.0


def test_top_level_row_value_handles_boolean_false_dimension():
    """edgartools' quarterly_financials dataframes encode top-level rows
    as ``dimension == False`` (it's a boolean flag, not a string).
    Regression guard for the live-smoke bug found 2026-05-25."""
    df = pd.DataFrame({
        "concept": ["us-gaap_InventoryNet"],
        "dimension": [False],
        "2026-04-26": [25_797_000_000.0],
    })
    assert _top_level_row_value(df, "us-gaap_InventoryNet", "2026-04-26") == 25_797_000_000.0


def test_top_level_row_value_excludes_boolean_true_dimension():
    """``dimension == True`` rows are segmented breakouts — MUST be excluded."""
    df = pd.DataFrame({
        "concept": ["us-gaap_CostOfRevenue", "us-gaap_CostOfRevenue"],
        "dimension": [False, True],
        "2026-04-26": [20_458_000_000.0, 5_000_000_000.0],
    })
    val = _top_level_row_value(df, "us-gaap_CostOfRevenue", "2026-04-26")
    assert val == 20_458_000_000.0


# ---------------------------------------------------------------------------
# _median
# ---------------------------------------------------------------------------


def test_median_odd_and_even():
    assert _median([1.0, 3.0, 2.0]) == 2.0
    assert _median([1.0, 3.0, 2.0, 4.0]) == 2.5


def test_median_empty_returns_none():
    assert _median([]) is None


# ---------------------------------------------------------------------------
# _classify_thesis_signal — four-bucket decision table
# ---------------------------------------------------------------------------


def test_classify_chip_supply_tight_when_both_signals_supportive():
    """Semis tape up + inventory builds modest → demand absorbing supply."""
    assert _classify_thesis_signal(
        semis_index_median_90d=10.0,
        median_inventory_qoq_change_pct=2.0,
    ) == "chip_supply_tight"


def test_classify_chip_supply_loose_when_semis_tape_weak():
    """Semis tape down >5% over 90d alone flips to loose."""
    assert _classify_thesis_signal(
        semis_index_median_90d=-8.0,
        median_inventory_qoq_change_pct=2.0,
    ) == "chip_supply_loose"


def test_classify_chip_supply_loose_when_inventory_builds_material():
    """Inventory QoQ change >=15% alone flips to loose, even if semis tape is strong."""
    assert _classify_thesis_signal(
        semis_index_median_90d=20.0,
        median_inventory_qoq_change_pct=20.0,
    ) == "chip_supply_loose"


def test_classify_mixed_when_signals_disagree_modestly():
    """Semis tape modestly positive (0-5%) BUT inventory building 5-15% =
    no decisive verdict either way → mixed."""
    assert _classify_thesis_signal(
        semis_index_median_90d=3.0,
        median_inventory_qoq_change_pct=10.0,
    ) == "mixed"


def test_classify_tight_when_one_signal_supportive_other_missing():
    """Single-signal cases: when one is missing and the other clears the
    tight bar, that's still tight (treat missing as neutral)."""
    assert _classify_thesis_signal(
        semis_index_median_90d=10.0,
        median_inventory_qoq_change_pct=None,
    ) == "chip_supply_tight"
    assert _classify_thesis_signal(
        semis_index_median_90d=None,
        median_inventory_qoq_change_pct=2.0,
    ) == "chip_supply_tight"


def test_classify_no_data_when_both_missing():
    assert _classify_thesis_signal(
        semis_index_median_90d=None,
        median_inventory_qoq_change_pct=None,
    ) == "no_data"


# ---------------------------------------------------------------------------
# _snapshot_from_data
# ---------------------------------------------------------------------------


def test_snapshot_combines_momentum_and_inventory():
    snap = _snapshot_from_data(
        symbol="NVDA",
        category="gpu",
        momentum=_momentum(last_close=1234.5, change_90d_pct=25.0),
        inventory=_inventory(),
    )
    assert snap.symbol == "NVDA"
    assert snap.last_close_usd == 1234.5
    assert snap.change_90d_pct == 25.0
    assert snap.inventory_latest_usd == 25_797_000_000.0
    assert snap.inventory_latest_period == "2026-04-26"
    assert snap.inventory_qoq_change_pct is not None
    assert abs(snap.inventory_qoq_change_pct - 20.53) < 0.1
    assert snap.inventory_days_latest is not None
    assert abs(snap.inventory_days_latest - 114.7) < 1.0
    assert snap.fetch_status == "ok"


def test_snapshot_etf_has_no_inventory_fields():
    """ETFs (SOXX/SMH) and foreign issuers (TSM) have momentum only —
    inventory fields stay None and fetch_status remains 'ok'."""
    snap = _snapshot_from_data(
        symbol="SOXX",
        category="semis_index",
        momentum=_momentum(),
        inventory=None,
    )
    assert snap.last_close_usd == 100.0
    assert snap.inventory_latest_usd is None
    assert snap.inventory_qoq_change_pct is None
    assert snap.inventory_days_latest is None
    assert snap.fetch_status == "ok"


# ---------------------------------------------------------------------------
# compose — full payload shape
# ---------------------------------------------------------------------------


def test_compose_returns_well_formed_payload():
    trace = compose(
        momentum_fetcher=_stub_momentum_fetcher(),
        inventory_fetcher=_stub_inventory_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    payload = trace.output
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["fetched_at"] == "2026-05-26T10:00:00+00:00"
    assert set(payload["categories"].keys()) == set(SEMI_TICKERS.keys())
    total = sum(len(v) for v in SEMI_TICKERS.values())
    assert payload["aggregate"]["n_attempted"] == total
    assert payload["aggregate"]["n_ok"] == total
    assert payload["errors"] == []
    written = {row["symbol"] for row in payload["symbols"]}
    expected = {s for syms in SEMI_TICKERS.values() for s in syms}
    assert written == expected


def test_compose_only_calls_inventory_fetcher_for_us_filers():
    """ETFs (SOXX, SMH) and TSM MUST NOT trigger an edgartools call."""
    called_for: list[str] = []

    def tracking_inv(ticker: str) -> dict:
        called_for.append(ticker)
        return _inventory()

    compose(
        momentum_fetcher=_stub_momentum_fetcher(),
        inventory_fetcher=tracking_inv,
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert set(called_for) == set(INVENTORY_FETCH_TICKERS)
    assert "SOXX" not in called_for
    assert "TSM" not in called_for


def test_compose_momentum_failure_marks_symbol_error_does_not_abort():
    trace = compose(
        momentum_fetcher=_stub_momentum_fetcher(raise_on={"SOXX"}),
        inventory_fetcher=_stub_inventory_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    payload = trace.output
    total = sum(len(v) for v in SEMI_TICKERS.values())
    assert payload["aggregate"]["n_attempted"] == total
    assert payload["aggregate"]["n_ok"] == total - 1
    soxx = next(r for r in payload["symbols"] if r["symbol"] == "SOXX")
    assert soxx["fetch_status"] == "error"
    assert soxx["error_reason"].startswith("momentum:")
    assert soxx["last_close_usd"] is None


def test_compose_inventory_failure_does_not_poison_momentum():
    """When inventory fetch fails for NVDA but momentum succeeded, the row
    stays fetch_status=ok with momentum populated, only inventory fields null."""
    trace = compose(
        momentum_fetcher=_stub_momentum_fetcher(),
        inventory_fetcher=_stub_inventory_fetcher(raise_on={"NVDA"}),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    payload = trace.output
    nvda = next(r for r in payload["symbols"] if r["symbol"] == "NVDA")
    assert nvda["fetch_status"] == "ok"  # momentum succeeded
    assert nvda["last_close_usd"] == 100.0
    assert nvda["inventory_latest_usd"] is None
    assert nvda["inventory_qoq_change_pct"] is None
    # The inventory failure shows up in errors[]
    assert any(
        e["symbol"] == "NVDA" and e["reason"].startswith("inventory:")
        for e in payload["errors"]
    )


def test_compose_aggregate_classifier_uses_semis_index_only_for_momentum_half():
    """The classifier looks at semis_index median 90d ONLY — strong NVDA
    momentum alone MUST NOT trigger 'tight' when SOXX/SMH are weak."""
    momentum_responses = {
        "SOXX": _momentum(change_90d_pct=-10.0),
        "SMH": _momentum(change_90d_pct=-8.0),
        "NVDA": _momentum(change_90d_pct=50.0),
        "AMD": _momentum(change_90d_pct=50.0),
        "MU": _momentum(change_90d_pct=50.0),
        "TSM": _momentum(change_90d_pct=50.0),
    }
    trace = compose(
        momentum_fetcher=_stub_momentum_fetcher(responses=momentum_responses),
        inventory_fetcher=_stub_inventory_fetcher(
            responses={
                t: _inventory(
                    inventory_latest_usd=100.0,
                    inventory_prior_usd=100.0,
                    cogs_latest_usd=200.0,
                )
                for t in ("NVDA", "AMD", "MU")
            }
        ),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    # semis_index median = (-10 + -8) / 2 = -9.0 → triggers chip_supply_loose
    assert trace.output["aggregate"]["thesis_signal"] == "chip_supply_loose"


def test_compose_inventory_qoq_aggregate_median_uses_only_ok_rows():
    """Errored / missing inventory rows MUST NOT skew the median."""
    inv_responses = {
        "NVDA": _inventory(
            inventory_latest_usd=25_797_000_000,
            inventory_prior_usd=21_403_000_000,
        ),  # ~+20.5%
        "AMD": _inventory(
            inventory_latest_usd=8_045_000_000,
            inventory_prior_usd=7_920_000_000,
        ),  # ~+1.6%
    }
    trace = compose(
        momentum_fetcher=_stub_momentum_fetcher(),
        inventory_fetcher=_stub_inventory_fetcher(
            responses=inv_responses,
            raise_on={"MU"},  # MU inventory fails → not counted
        ),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    median_qoq = trace.output["aggregate"]["median_inventory_qoq_change_pct"]
    # Median of NVDA +20.5% and AMD +1.6% = ~11.0%
    assert median_qoq is not None
    assert 10.0 < median_qoq < 12.0


def test_compose_writes_json_when_out_path_set(tmp_path: Path):
    out = tmp_path / "subdir" / "semiconductor_inventory.json"
    trace = compose(
        out_path=out,
        momentum_fetcher=_stub_momentum_fetcher(),
        inventory_fetcher=_stub_inventory_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert out.exists()
    on_disk = json.loads(out.read_text())
    assert on_disk == trace.output


def test_compose_does_not_write_when_out_path_none(tmp_path: Path):
    compose(
        out_path=None,
        momentum_fetcher=_stub_momentum_fetcher(),
        inventory_fetcher=_stub_inventory_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert list(tmp_path.iterdir()) == []


def test_compose_accepts_tickers_and_inventory_set_override():
    mini = {"semis_index": ["SOXX"], "gpu": ["NVDA"]}
    trace = compose(
        tickers=mini,
        inventory_fetch_tickers=frozenset({"NVDA"}),
        momentum_fetcher=_stub_momentum_fetcher(),
        inventory_fetcher=_stub_inventory_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    payload = trace.output
    assert payload["aggregate"]["n_attempted"] == 2
    assert payload["categories"] == mini
    assert payload["inventory_fetch_tickers"] == ["NVDA"]


def test_compose_emits_traceentry_for_ledger_embedding():
    trace = compose(
        momentum_fetcher=_stub_momentum_fetcher(),
        inventory_fetcher=_stub_inventory_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert trace.tool == "tools/thematic_portfolio/tier3/semiconductor_inventory.py"
    assert "n_categories" in trace.inputs
    assert "n_inventory_fetch_tickers" in trace.inputs
    assert trace.fetched_at


def test_compose_category_assignment_matches_catalog():
    trace = compose(
        momentum_fetcher=_stub_momentum_fetcher(),
        inventory_fetcher=_stub_inventory_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    expected_category_of: dict[str, str] = {}
    for cat, syms in SEMI_TICKERS.items():
        for s in syms:
            expected_category_of[s] = cat
    for row in trace.output["symbols"]:
        assert row["category"] == expected_category_of[row["symbol"]]
