"""Tests for tools.fundamentals.edgar_eps — TTM EPS lookup adapter.

All tests inject a fake Company-like object via the ``_company_factory``
test seam — no live SEC EDGAR calls are made.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from tools.fundamentals.edgar_eps import (
    EdgarEPSError,
    EPSResult,
    compute,
    fetch_ttm_eps,
)


# ---- fakes --------------------------------------------------------------


def _fake_ttm(value, periods=None):
    return SimpleNamespace(
        value=value,
        periods=periods or [(2025, "Q3"), (2025, "Q4"), (2026, "Q1"), (2026, "Q2")],
        concept="us-gaap:NetIncomeLoss",
    )


def _fake_financials(diluted):
    return SimpleNamespace(
        get_shares_outstanding_diluted=lambda: diluted,
    )


def _make_company_factory(*, ttm=None, diluted=None, raise_on=None):
    """Return a factory that builds a fake Company exposing the methods
    fetch_ttm_eps calls. ``raise_on`` triggers raises on specific methods."""
    raise_on = raise_on or set()

    class FakeCompany:
        def __init__(self, _ticker):
            self.cik = 320193

        def get_ttm_net_income(self):
            if "get_ttm_net_income" in raise_on:
                raise RuntimeError("EDGAR ttm boom")
            return ttm

        def get_financials(self):
            if "get_financials" in raise_on:
                raise RuntimeError("EDGAR fin boom")
            return _fake_financials(diluted) if diluted is not None else None

    def _factory(ticker):
        if "Company" in raise_on:
            raise RuntimeError("EDGAR company lookup boom")
        return FakeCompany(ticker)

    return _factory


# ---- success ------------------------------------------------------------


def test_fetch_ttm_eps_success(tmp_path):
    factory = _make_company_factory(ttm=_fake_ttm(12_500_000_000), diluted=2_500_000_000.0)
    res = fetch_ttm_eps("XYZ", cache_dir=tmp_path, _company_factory=factory)
    assert isinstance(res, EPSResult)
    assert res.ticker == "XYZ"
    assert res.ttm_eps == pytest.approx(5.00, rel=1e-9)
    assert res.ttm_net_income == 12_500_000_000.0
    assert res.diluted_shares == 2_500_000_000.0
    assert "Q3" in res.period_label
    assert "edgartools:" in res.source


def test_compute_wraps_in_trace_entry(tmp_path):
    factory = _make_company_factory(ttm=_fake_ttm(10_000_000_000), diluted=2_000_000_000.0)
    entry = compute("AAA", cache_dir=tmp_path, _company_factory=factory)
    assert entry.tool == "tools/fundamentals/edgar_eps.py"
    assert entry.inputs == {"ticker": "AAA"}
    assert entry.output["ttm_eps"] == pytest.approx(5.0)
    assert entry.fetched_at  # ISO string set


def test_ticker_is_uppercased(tmp_path):
    factory = _make_company_factory(ttm=_fake_ttm(1e9), diluted=1e8)
    res = fetch_ttm_eps("aapl", cache_dir=tmp_path, _company_factory=factory)
    assert res.ticker == "AAPL"


# ---- cache --------------------------------------------------------------


def test_cache_hit_serves_without_factory_call(tmp_path):
    """A populated cache returns the cached EPSResult without calling
    the factory at all — so we can pass a factory that would raise."""
    factory_seed = _make_company_factory(ttm=_fake_ttm(1e10), diluted=2e9)
    fetch_ttm_eps("CACHED", cache_dir=tmp_path, _company_factory=factory_seed)

    # If cache was honored, a raising factory must NOT be called.
    factory_boom = _make_company_factory(raise_on={"Company"})
    res = fetch_ttm_eps("CACHED", cache_dir=tmp_path, _company_factory=factory_boom)
    assert res.ticker == "CACHED"


def test_cache_bypass_with_use_cache_false(tmp_path):
    factory_seed = _make_company_factory(ttm=_fake_ttm(1e10), diluted=2e9)
    fetch_ttm_eps("BYPASS", cache_dir=tmp_path, _company_factory=factory_seed)

    factory_boom = _make_company_factory(raise_on={"Company"})
    with pytest.raises(EdgarEPSError):
        fetch_ttm_eps("BYPASS", cache_dir=tmp_path, use_cache=False,
                      _company_factory=factory_boom)


def test_stale_cache_is_refetched(tmp_path):
    """When the cache entry is older than CACHE_TTL_SECONDS, fetch hits the factory."""
    factory_old = _make_company_factory(ttm=_fake_ttm(1e10), diluted=2e9)
    fetch_ttm_eps("STALE", cache_dir=tmp_path, _company_factory=factory_old)

    # Backdate the cache file so it looks > 24h old.
    p = tmp_path / "STALE.json"
    data = json.loads(p.read_text())
    data["_cached_at_epoch"] = time.time() - 100_000  # ~27.7h ago
    p.write_text(json.dumps(data))

    # A different factory output proves we re-fetched.
    factory_new = _make_company_factory(ttm=_fake_ttm(2e10), diluted=2e9)
    res = fetch_ttm_eps("STALE", cache_dir=tmp_path, _company_factory=factory_new)
    assert res.ttm_eps == pytest.approx(10.0)  # 2e10/2e9


def test_malformed_cache_falls_through(tmp_path):
    cache_dir = tmp_path
    cache_dir.mkdir(exist_ok=True)
    (cache_dir / "ZZZ.json").write_text("not valid json{{{")

    factory = _make_company_factory(ttm=_fake_ttm(1e10), diluted=2e9)
    res = fetch_ttm_eps("ZZZ", cache_dir=cache_dir, _company_factory=factory)
    assert res.ttm_eps == pytest.approx(5.0)


# ---- failure modes ------------------------------------------------------


def test_company_lookup_failure_raises(tmp_path):
    factory = _make_company_factory(raise_on={"Company"})
    with pytest.raises(EdgarEPSError, match="company lookup failed"):
        fetch_ttm_eps("BAD", cache_dir=tmp_path, _company_factory=factory)


def test_missing_ttm_raises(tmp_path):
    factory = _make_company_factory(ttm=None, diluted=2e9)
    with pytest.raises(EdgarEPSError, match="no TTM net income"):
        fetch_ttm_eps("NOTTM", cache_dir=tmp_path, _company_factory=factory)


def test_ttm_call_raises(tmp_path):
    factory = _make_company_factory(raise_on={"get_ttm_net_income"})
    with pytest.raises(EdgarEPSError, match="TTM net income lookup failed"):
        fetch_ttm_eps("FAIL", cache_dir=tmp_path, _company_factory=factory)


def test_missing_diluted_shares_raises(tmp_path):
    factory = _make_company_factory(ttm=_fake_ttm(1e10), diluted=None)
    with pytest.raises(EdgarEPSError, match="no financials"):
        fetch_ttm_eps("NODIL", cache_dir=tmp_path, _company_factory=factory)


def test_zero_diluted_shares_raises(tmp_path):
    factory = _make_company_factory(ttm=_fake_ttm(1e10), diluted=0)
    with pytest.raises(EdgarEPSError, match="non-positive diluted shares"):
        fetch_ttm_eps("ZERO", cache_dir=tmp_path, _company_factory=factory)


def test_negative_ttm_returns_negative_eps(tmp_path):
    """Loss-making company: TTM net income negative, EPS negative. Adapter
    returns the value; pe_expansion_check is what guards against negative EPS."""
    factory = _make_company_factory(ttm=_fake_ttm(-5_000_000_000), diluted=2e9)
    res = fetch_ttm_eps("LOSS", cache_dir=tmp_path, _company_factory=factory)
    assert res.ttm_eps == pytest.approx(-2.5)
