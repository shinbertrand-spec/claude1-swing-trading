"""Tests for tools.thematic_portfolio.tier3.energy_futures."""
from __future__ import annotations

import json
from pathlib import Path

from tools.thematic_portfolio.tier3.energy_futures import (
    ENERGY_SYMBOLS,
    SCHEMA_VERSION,
    SymbolSnapshot,
    _classify_thesis_signal,
    _median,
    _snapshot_from_history,
    compose,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _hist(
    *,
    last_close=100.0,
    change_30d_pct=5.0,
    change_90d_pct=12.0,
    change_ytd_pct=20.0,
) -> dict:
    return {
        "last_close": last_close,
        "change_30d_pct": change_30d_pct,
        "change_90d_pct": change_90d_pct,
        "change_ytd_pct": change_ytd_pct,
    }


def _stub_fetcher(responses: dict[str, dict] | None = None, raise_on: set[str] | None = None):
    """Build a per-symbol history stub.

    ``responses`` maps symbol -> ``hist`` dict. Missing symbols get a
    generic flat-positive default. ``raise_on`` set tells the fetcher to
    raise for those symbols (simulating yfinance errors).
    """
    responses = responses or {}
    raise_on = raise_on or set()

    def _fn(symbol: str) -> dict:
        if symbol in raise_on:
            raise RuntimeError(f"simulated yfinance failure for {symbol}")
        return responses.get(symbol, _hist())

    return _fn


# ---------------------------------------------------------------------------
# Catalog discipline
# ---------------------------------------------------------------------------


def test_catalog_categories_are_the_four_v1_classes():
    """The four categories encode input-identity-by-role; adding/removing a
    category is a design change, not maintenance."""
    assert set(ENERGY_SYMBOLS.keys()) == {
        "natgas",
        "uranium",
        "crude_oil",
        "power_proxy",
    }


def test_catalog_symbols_are_uppercase_and_unique():
    seen: set[str] = set()
    for cat, syms in ENERGY_SYMBOLS.items():
        for s in syms:
            # Allow "=" for futures suffix (NG=F, CL=F)
            assert s == s.upper(), f"{cat}:{s} should be uppercase"
            assert s not in seen, f"duplicate symbol {s} across categories"
            seen.add(s)


def test_catalog_includes_henry_hub_natgas_front_month():
    """NG=F is the load-bearing natgas signal — Henry Hub continuous
    front-month — and MUST be present."""
    assert "NG=F" in ENERGY_SYMBOLS["natgas"]


def test_catalog_includes_uranium_etfs_for_sa_lp_nuclear_thesis():
    """SA LP's power thesis leans on the nuclear-restart pivot. URA + URNM
    are the two retail-accessible uranium-equity ETFs and MUST be present."""
    uranium = set(ENERGY_SYMBOLS["uranium"])
    assert "URA" in uranium
    assert "URNM" in uranium


def test_catalog_includes_utility_etfs_as_power_price_proxy():
    """There is no retail-accessible PJM-W / ERCOT power-price feed. XLU is
    the closest no-key proxy + MUST be present."""
    assert "XLU" in ENERGY_SYMBOLS["power_proxy"]


# ---------------------------------------------------------------------------
# _snapshot_from_history — fetcher-output normalization
# ---------------------------------------------------------------------------


def test_snapshot_from_history_happy_path():
    snap = _snapshot_from_history("NG=F", "natgas", _hist(
        last_close=3.42, change_30d_pct=8.1, change_90d_pct=14.2, change_ytd_pct=-2.3,
    ))
    assert snap.symbol == "NG=F"
    assert snap.category == "natgas"
    assert snap.last_close_usd == 3.42
    assert snap.change_30d_pct == 8.1
    assert snap.change_90d_pct == 14.2
    assert snap.change_ytd_pct == -2.3
    assert snap.fetch_status == "ok"


def test_snapshot_from_history_handles_partial_lookbacks():
    """For new listings, the 90d / YTD lookbacks may be None (not enough
    history); fetch_status stays 'ok' because None is data-absence, not error."""
    snap = _snapshot_from_history("X", "natgas", {
        "last_close": 50.0,
        "change_30d_pct": 4.0,
        "change_90d_pct": None,
        "change_ytd_pct": None,
    })
    assert snap.last_close_usd == 50.0
    assert snap.change_30d_pct == 4.0
    assert snap.change_90d_pct is None
    assert snap.change_ytd_pct is None
    assert snap.fetch_status == "ok"


# ---------------------------------------------------------------------------
# _median helper
# ---------------------------------------------------------------------------


def test_median_odd_length():
    assert _median([1.0, 3.0, 2.0]) == 2.0


def test_median_even_length():
    assert _median([1.0, 3.0, 2.0, 4.0]) == 2.5


def test_median_empty_returns_none():
    assert _median([]) is None


# ---------------------------------------------------------------------------
# _classify_thesis_signal
# ---------------------------------------------------------------------------


def test_classify_supportive_when_two_core_categories_strong():
    """natgas +10 AND uranium +8 → supportive (2 of 3 core categories
    above +5%)."""
    medians = {"natgas": 10.0, "uranium": 8.0, "power_proxy": 1.0, "crude_oil": -2.0}
    assert _classify_thesis_signal(medians) == "supportive"


def test_classify_weakening_when_two_core_categories_weak():
    """natgas -8 AND uranium -12 → weakening (2 of 3 core categories
    below -5%)."""
    medians = {"natgas": -8.0, "uranium": -12.0, "power_proxy": 2.0, "crude_oil": -5.0}
    assert _classify_thesis_signal(medians) == "weakening"


def test_classify_mixed_when_signals_disagree():
    """natgas +10 BUT uranium -10 → mixed (no 2-of-3 majority either way)."""
    medians = {"natgas": 10.0, "uranium": -10.0, "power_proxy": 0.0}
    assert _classify_thesis_signal(medians) == "mixed"


def test_classify_no_data_when_all_core_missing():
    medians = {"natgas": None, "uranium": None, "power_proxy": None, "crude_oil": 5.0}
    assert _classify_thesis_signal(medians) == "no_data"


def test_classify_crude_oil_does_not_drive_classification():
    """crude_oil is reference-context only — it MUST NOT be counted in the
    AI-power-thesis classifier even when very strong."""
    medians = {"natgas": 0.0, "uranium": 0.0, "power_proxy": 0.0, "crude_oil": 30.0}
    # All three core categories are flat-ish → mixed (not supportive)
    assert _classify_thesis_signal(medians) == "mixed"


# ---------------------------------------------------------------------------
# compose — full payload shape
# ---------------------------------------------------------------------------


def test_compose_returns_well_formed_payload():
    trace = compose(
        yf_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    payload = trace.output
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["fetched_at"] == "2026-05-26T10:00:00+00:00"
    assert set(payload["categories"].keys()) == set(ENERGY_SYMBOLS.keys())
    total = sum(len(v) for v in ENERGY_SYMBOLS.values())
    assert payload["aggregate"]["n_attempted"] == total
    assert payload["aggregate"]["n_ok"] == total
    assert payload["errors"] == []
    written = {row["symbol"] for row in payload["symbols"]}
    all_expected = {s for syms in ENERGY_SYMBOLS.values() for s in syms}
    assert written == all_expected


def test_compose_per_symbol_failure_does_not_abort_run():
    """yfinance failures on one symbol MUST NOT abort the rest."""
    trace = compose(
        yf_fetcher=_stub_fetcher(raise_on={"NG=F", "URA"}),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    payload = trace.output
    total = sum(len(v) for v in ENERGY_SYMBOLS.values())
    assert payload["aggregate"]["n_attempted"] == total
    assert payload["aggregate"]["n_ok"] == total - 2
    assert {e["symbol"] for e in payload["errors"]} == {"NG=F", "URA"}
    rows = {row["symbol"]: row for row in payload["symbols"]}
    assert rows["NG=F"]["fetch_status"] == "error"
    assert rows["NG=F"]["error_reason"].startswith("RuntimeError")
    assert rows["NG=F"]["last_close_usd"] is None


def test_compose_median_change_90d_per_category_uses_only_ok_rows():
    """Errored symbols MUST NOT contribute to the per-category median."""
    responses = {
        "NG=F": _hist(change_90d_pct=10.0),
        "UNG": _hist(change_90d_pct=20.0),
        "UNL": _hist(change_90d_pct=30.0),
    }
    trace = compose(
        symbols={"natgas": ["NG=F", "UNG", "UNL"]},
        yf_fetcher=_stub_fetcher(responses=responses, raise_on={"UNL"}),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    medians = trace.output["aggregate"]["median_change_90d_pct_by_category"]
    # median of [10.0, 20.0] = 15.0 (UNL excluded)
    assert medians["natgas"] == 15.0


def test_compose_aggregate_thesis_signal_supportive_path():
    """When natgas + uranium medians both >= +5% → supportive."""
    catalog = {
        "natgas": ["NG=F"],
        "uranium": ["URA"],
        "power_proxy": ["XLU"],
    }
    responses = {
        "NG=F": _hist(change_90d_pct=10.0),
        "URA": _hist(change_90d_pct=15.0),
        "XLU": _hist(change_90d_pct=1.0),
    }
    trace = compose(
        symbols=catalog,
        yf_fetcher=_stub_fetcher(responses=responses),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert trace.output["aggregate"]["thesis_signal"] == "supportive"


def test_compose_writes_json_when_out_path_set(tmp_path: Path):
    out = tmp_path / "subdir" / "energy_futures.json"
    trace = compose(
        out_path=out,
        yf_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert out.exists()
    on_disk = json.loads(out.read_text())
    assert on_disk["schema_version"] == SCHEMA_VERSION
    assert on_disk == trace.output


def test_compose_does_not_write_when_out_path_none(tmp_path: Path):
    trace = compose(
        out_path=None,
        yf_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert list(tmp_path.iterdir()) == []
    assert trace.output["aggregate"]["n_ok"] > 0


def test_compose_accepts_symbols_override():
    mini = {"natgas": ["NG=F"], "uranium": ["URA"]}
    trace = compose(
        symbols=mini,
        yf_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    payload = trace.output
    assert payload["aggregate"]["n_attempted"] == 2
    assert payload["categories"] == mini
    assert {row["symbol"] for row in payload["symbols"]} == {"NG=F", "URA"}


def test_compose_emits_traceentry_for_ledger_embedding():
    trace = compose(
        yf_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert trace.tool == "tools/thematic_portfolio/tier3/energy_futures.py"
    assert "n_categories" in trace.inputs
    assert trace.fetched_at  # populated by dataclass default_factory


def test_compose_category_assignment_matches_catalog():
    """Each emitted row's category field must match the catalog's grouping."""
    trace = compose(
        yf_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    expected_category_of: dict[str, str] = {}
    for cat, syms in ENERGY_SYMBOLS.items():
        for s in syms:
            expected_category_of[s] = cat
    for row in trace.output["symbols"]:
        assert row["category"] == expected_category_of[row["symbol"]]
