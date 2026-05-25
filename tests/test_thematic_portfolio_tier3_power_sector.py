"""Tests for tools.thematic_portfolio.tier3.power_sector."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.thematic_portfolio.tier3.power_sector import (
    POWER_TICKERS,
    SCHEMA_VERSION,
    TickerSnapshot,
    _snapshot_from_info,
    compose,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _info(
    *,
    price=100.0,
    market_cap=1_000_000_000.0,
    trailing_pe=20.0,
    trailing_eps=5.0,
    next_earnings_date="2026-07-30",
) -> dict:
    return {
        "regularMarketPrice": price,
        "marketCap": market_cap,
        "trailingPE": trailing_pe,
        "trailingEps": trailing_eps,
        "_next_earnings_date": next_earnings_date,
    }


def _stub_fetcher(responses: dict[str, dict] | None = None, raise_on: set[str] | None = None):
    """Build a per-ticker yfinance stub.

    ``responses`` maps ticker -> ``info`` dict. Missing tickers get a
    generic 100-EPS-5 default. ``raise_on`` set tells the fetcher to
    raise for those tickers (simulating yfinance errors).
    """
    responses = responses or {}
    raise_on = raise_on or set()

    def _fn(ticker: str) -> dict:
        if ticker in raise_on:
            raise RuntimeError(f"simulated yfinance failure for {ticker}")
        return responses.get(ticker, _info())

    return _fn


# ---------------------------------------------------------------------------
# Catalog discipline
# ---------------------------------------------------------------------------


def test_catalog_categories_are_the_four_v1_classes():
    """The four categories encode fund-identity-by-role (per session-2 #6).
    Adding/removing a category is a design change, not a maintenance edit."""
    assert set(POWER_TICKERS.keys()) == {
        "hyperscaler",
        "utility_data_center_exposed",
        "power_infra_equipment",
        "miner_pivot",
    }


def test_catalog_tickers_are_uppercase_and_unique():
    seen: set[str] = set()
    for cat, tickers in POWER_TICKERS.items():
        for t in tickers:
            assert t == t.upper(), f"{cat}:{t} should be uppercase"
            assert t not in seen, f"duplicate ticker {t} across categories"
            seen.add(t)


def test_catalog_includes_key_sa_lp_positions_in_miner_pivot():
    """The miner-pivot list MUST cover the SA LP names whose thesis the
    power_sector compiler corroborates — APLD, IREN, CORZ are top-10
    SA LP positions by weight per the canonical Q1 2026 13F."""
    miners = set(POWER_TICKERS["miner_pivot"])
    for required in ("APLD", "IREN", "CORZ"):
        assert required in miners


def test_catalog_includes_be_in_power_infra():
    """BE (Bloom Energy) is SA LP's #1 long at 22.79% of long book; it
    MUST be in the curated power-infra group so the compiler surfaces
    its fundamentals to Loop 1."""
    assert "BE" in POWER_TICKERS["power_infra_equipment"]


# ---------------------------------------------------------------------------
# _snapshot_from_info — yfinance normalization
# ---------------------------------------------------------------------------


def test_snapshot_from_info_happy_path():
    snap = _snapshot_from_info("CEG", "utility_data_center_exposed", _info(
        price=277.22, market_cap=88_200_000_000, trailing_pe=22.5, trailing_eps=12.30,
    ))
    assert snap.ticker == "CEG"
    assert snap.price_usd == 277.22
    assert snap.market_cap_usd == 88_200_000_000.0
    assert snap.trailing_pe == 22.5
    assert snap.trailing_eps == 12.30
    assert snap.next_earnings_date == "2026-07-30"
    assert snap.fetch_status == "ok"


def test_snapshot_from_info_falls_back_to_previousClose_when_price_missing():
    snap = _snapshot_from_info("X", "hyperscaler", {
        "previousClose": 95.0,
        "marketCap": None,
        "trailingPE": None,
        "trailingEps": None,
    })
    assert snap.price_usd == 95.0
    assert snap.market_cap_usd is None
    assert snap.trailing_pe is None


def test_snapshot_from_info_handles_all_missing_fields():
    snap = _snapshot_from_info("X", "hyperscaler", {})
    assert snap.price_usd is None
    assert snap.market_cap_usd is None
    assert snap.trailing_pe is None
    assert snap.trailing_eps is None
    assert snap.next_earnings_date is None
    assert snap.fetch_status == "ok"  # missing data is NOT an error


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
    assert set(payload["categories"].keys()) == set(POWER_TICKERS.keys())
    assert payload["n_tickers_attempted"] == sum(len(v) for v in POWER_TICKERS.values())
    assert payload["n_tickers_ok"] == payload["n_tickers_attempted"]
    assert payload["errors"] == []
    # Every ticker shows up exactly once in the flattened list
    written = {row["ticker"] for row in payload["tickers"]}
    all_expected = {t for ts in POWER_TICKERS.values() for t in ts}
    assert written == all_expected


def test_compose_per_ticker_failure_does_not_abort_run():
    """yfinance failures on one ticker MUST NOT abort the rest. The
    failing ticker still appears with fetch_status=error."""
    trace = compose(
        yf_fetcher=_stub_fetcher(raise_on={"BE", "APLD"}),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    payload = trace.output
    total = sum(len(v) for v in POWER_TICKERS.values())
    assert payload["n_tickers_attempted"] == total
    assert payload["n_tickers_ok"] == total - 2
    assert {e["ticker"] for e in payload["errors"]} == {"BE", "APLD"}
    # The errored tickers still appear in `tickers[]` so the LLM sees the gap
    rows = {row["ticker"]: row for row in payload["tickers"]}
    assert rows["BE"]["fetch_status"] == "error"
    assert rows["BE"]["error_reason"].startswith("RuntimeError")
    assert rows["BE"]["price_usd"] is None


def test_compose_writes_json_when_out_path_set(tmp_path: Path):
    out = tmp_path / "subdir" / "power_sector.json"
    trace = compose(
        out_path=out,
        yf_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert out.exists()
    on_disk = json.loads(out.read_text())
    # Same shape as the in-memory payload
    assert on_disk["schema_version"] == SCHEMA_VERSION
    assert on_disk == trace.output


def test_compose_does_not_write_when_out_path_none(tmp_path: Path):
    """``out_path=None`` is the dry-run path."""
    trace = compose(
        out_path=None,
        yf_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert list(tmp_path.iterdir()) == []
    assert trace.output["n_tickers_ok"] > 0  # still produced the payload


def test_compose_accepts_tickers_override():
    """Tests can pass a smaller catalog to keep the per-ticker fetch
    count manageable."""
    mini = {"hyperscaler": ["MSFT"], "miner_pivot": ["APLD"]}
    trace = compose(
        tickers=mini,
        yf_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    payload = trace.output
    assert payload["n_tickers_attempted"] == 2
    assert payload["categories"] == mini
    assert {row["ticker"] for row in payload["tickers"]} == {"MSFT", "APLD"}


def test_compose_emits_traceentry_for_ledger_embedding():
    trace = compose(
        yf_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    assert trace.tool == "tools/thematic_portfolio/tier3/power_sector.py"
    assert "n_categories" in trace.inputs
    assert trace.fetched_at  # populated by dataclass default_factory


def test_compose_category_assignment_matches_catalog():
    """Each ticker's `category` field must match POWER_TICKERS membership."""
    trace = compose(
        yf_fetcher=_stub_fetcher(),
        now_iso_fn=lambda: "2026-05-26T10:00:00+00:00",
    )
    for row in trace.output["tickers"]:
        assert row["ticker"] in POWER_TICKERS[row["category"]]
