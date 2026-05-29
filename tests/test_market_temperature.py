"""Tests for tools.news_research.market_temperature.

Coverage:
- 4 fetchers — one happy-path + one network-error fail-soft each
- Cache hit + cache miss
- Schema validation against the bumped snapshot schema (1.3)
- All-fetchers-fail integration → composer returns block with errors[]
"""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from tools.news_research import market_temperature as mt


# ---------------------------------------------------------------------------
# Stubbed HTTP helpers
# ---------------------------------------------------------------------------


def _fail_http(url: str) -> bytes:  # noqa: ARG001
    raise urllib.error.URLError("simulated network failure")


def _make_dispatch(map_: dict[str, bytes]):
    def _dispatch(url: str) -> bytes:
        for key, body in map_.items():
            if key in url:
                return body
        raise AssertionError(f"unexpected url {url}")
    return _dispatch


# ---------------------------------------------------------------------------
# Fetcher: Fear & Greed
# ---------------------------------------------------------------------------


def test_fear_greed_happy_path():
    body = json.dumps({
        "fear_and_greed": {
            "score": 72.4,
            "rating": "greed",
            "timestamp": "2026-05-29T20:00:00+00:00",
        }
    }).encode()
    out = mt.fetch_fear_greed(http_get=lambda url: body)
    assert out["value"] == 72
    assert out["regime"] == "greed"
    assert out["source"] == "cnn"
    assert out["as_of"] == "2026-05-29T20:00:00+00:00"
    assert "error" not in out


def test_fear_greed_network_failure_fail_soft():
    out = mt.fetch_fear_greed(http_get=_fail_http)
    assert "error" in out
    assert out["as_of"] is None


# ---------------------------------------------------------------------------
# Fetcher: Put-Call
# ---------------------------------------------------------------------------


def test_put_call_happy_path():
    csv = (
        "Date,Total Put/Call Ratio,Equity Put/Call Ratio\n"
        "05/28/2026,0.92,0.65\n"
        "05/29/2026,1.05,0.71\n"
    ).encode()
    out = mt.fetch_put_call_ratio(http_get=lambda url: csv)
    assert out["total"] == 1.05
    assert out["equity"] == 0.71
    assert out["source"] == "cboe"
    assert out["as_of"] == "2026-05-29"
    assert "error" not in out


def test_put_call_network_failure_fail_soft():
    out = mt.fetch_put_call_ratio(http_get=_fail_http)
    assert "error" in out
    assert out["as_of"] is None


# ---------------------------------------------------------------------------
# Fetcher: AAII
# ---------------------------------------------------------------------------


def test_aaii_happy_path():
    html = b"""
    <html><body>
    <p>Reported survey period: 5/28/2026</p>
    <table>
      <tr><td>Bullish</td><td>34.5%</td></tr>
      <tr><td>Neutral</td><td>30.0%</td></tr>
      <tr><td>Bearish</td><td>35.5%</td></tr>
    </table>
    </body></html>
    """
    out = mt.fetch_aaii_survey(http_get=lambda url: html)
    assert abs(out["bull"] - 0.345) < 1e-9
    assert abs(out["neutral"] - 0.30) < 1e-9
    assert abs(out["bear"] - 0.355) < 1e-9
    assert out["source"] == "aaii"
    assert out["as_of_week"] == "2026-05-28"
    assert "error" not in out


def test_aaii_network_failure_fail_soft():
    out = mt.fetch_aaii_survey(http_get=_fail_http)
    assert "error" in out
    assert out["as_of"] is None


# ---------------------------------------------------------------------------
# Fetcher: VIX term structure
# ---------------------------------------------------------------------------


def test_vix_term_happy_path_contango():
    vix_csv = b"Date,Open,High,Low,Close\n05/29/2026,18.5,19.0,18.0,18.0\n"
    vix9d_csv = b"Date,Open,High,Low,Close\n05/29/2026,17.0,17.5,16.8,17.0\n"
    vix3m_csv = b"Date,Open,High,Low,Close\n05/29/2026,21.0,21.5,20.5,21.0\n"
    dispatch = _make_dispatch({
        "VIX_History": vix_csv,
        "VIX9D_History": vix9d_csv,
        "VIX3M_History": vix3m_csv,
    })
    out = mt.fetch_vix_term_structure(http_get=dispatch)
    assert out["vix"] == 18.0
    assert out["vix9d"] == 17.0
    assert out["vix3m"] == 21.0
    assert out["regime"] == "contango"  # vix/vix3m = 0.857 < 0.95
    assert out["source"] == "cboe"
    assert "error" not in out


def test_vix_term_network_failure_fail_soft():
    out = mt.fetch_vix_term_structure(http_get=_fail_http)
    assert "error" in out
    assert out["as_of"] is None


def test_vix_term_regimes():
    """Regime classification covers each ladder rung."""
    # backwardation: vix/vix3m > 1.05
    assert mt._classify_vix_term(vix=30.0, vix9d=29.0, vix3m=25.0) == "backwardation"
    # short_term_stress: vix9d/vix > 1.10
    assert mt._classify_vix_term(vix=15.0, vix9d=17.0, vix3m=16.0) == "short_term_stress"
    # neutral
    assert mt._classify_vix_term(vix=18.0, vix9d=18.5, vix3m=18.0) == "neutral"


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------


def test_cache_miss_then_hit(tmp_path: Path):
    cache_dir = tmp_path / "mt_cache"
    body = json.dumps({
        "fear_and_greed": {"score": 60, "timestamp": "2026-05-29T20:00:00+00:00"}
    }).encode()

    calls = {"n": 0}

    def http(url: str) -> bytes:
        calls["n"] += 1
        return body

    # MISS — populates the cache.
    block_a = mt._cached_fetch(
        "fear_greed", http_get=http, cache_dir=cache_dir, now_fn=lambda: 1_000_000.0,
    )
    assert "error" not in block_a
    assert calls["n"] == 1
    assert mt._cache_path("fear_greed", cache_dir=cache_dir).is_file()

    # HIT — within TTL → no second HTTP call.
    block_b = mt._cached_fetch(
        "fear_greed", http_get=http, cache_dir=cache_dir, now_fn=lambda: 1_000_100.0,
    )
    assert block_b == block_a
    assert calls["n"] == 1


def test_cache_expires_after_ttl(tmp_path: Path):
    """Cache entry beyond TTL → refetch."""
    cache_dir = tmp_path / "mt_cache"
    body = json.dumps({
        "fear_and_greed": {"score": 60, "timestamp": "2026-05-29T20:00:00+00:00"}
    }).encode()
    calls = {"n": 0}

    def http(url: str) -> bytes:
        calls["n"] += 1
        return body

    mt._cached_fetch(
        "fear_greed", http_get=http, cache_dir=cache_dir, now_fn=lambda: 0.0,
    )
    # Beyond 3600s TTL → refetch.
    mt._cached_fetch(
        "fear_greed", http_get=http, cache_dir=cache_dir, now_fn=lambda: 4_000.0,
    )
    assert calls["n"] == 2


def test_cache_does_not_store_error_values(tmp_path: Path):
    cache_dir = tmp_path / "mt_cache"
    mt._cached_fetch(
        "fear_greed", http_get=_fail_http, cache_dir=cache_dir, now_fn=lambda: 0.0,
    )
    assert not mt._cache_path("fear_greed", cache_dir=cache_dir).is_file()


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def test_composer_all_fetchers_fail_soft(tmp_path: Path):
    block = mt.fetch_market_temperature(
        http_get=_fail_http,
        cache_dir=tmp_path / "mt_cache",
        now_fn=lambda: 0.0,
        use_cache=False,
    )
    # Never raises, all four children carry errors, errors[] populated.
    assert set(block.keys()) >= {
        "as_of", "put_call", "fear_greed", "aaii", "vix_term", "errors",
    }
    assert len(block["errors"]) == 4
    for k in ("put_call", "fear_greed", "aaii", "vix_term"):
        assert "error" in block[k]


def test_composer_partial_success(tmp_path: Path):
    """One success, three failures — composer surfaces both."""
    fg_body = json.dumps({
        "fear_and_greed": {"score": 50, "timestamp": "2026-05-29T20:00:00+00:00"}
    }).encode()

    def http(url: str) -> bytes:
        if "fearandgreed" in url:
            return fg_body
        raise urllib.error.URLError("nope")

    block = mt.fetch_market_temperature(
        http_get=http,
        cache_dir=tmp_path / "mt_cache",
        now_fn=lambda: 0.0,
        use_cache=False,
    )
    assert "error" not in block["fear_greed"]
    assert block["fear_greed"]["value"] == 50
    assert len(block["errors"]) == 3


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_composer_conforms_to_snapshot_schema_1_3(tmp_path: Path):
    """Composer output validates against the news_snapshot 1.3 fragment."""
    import jsonschema

    schema_path = (
        Path(__file__).resolve().parents[1]
        / "ledgers" / "news" / "_schema" / "news_snapshot.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    # The block sub-schema must be present + reachable for v1.3.
    assert "market_temperature" in schema["$defs"], \
        "schema bump 1.2 -> 1.3 must define $defs.market_temperature"

    fg_body = json.dumps({
        "fear_and_greed": {"score": 50, "timestamp": "2026-05-29T20:00:00+00:00"}
    }).encode()
    pc_csv = b"Date,Total Put/Call Ratio\n05/29/2026,0.95\n"
    aaii_html = b"""Reported: 5/28/2026
        Bullish 30% Neutral 40% Bearish 30%"""
    vix_csv = b"Date,Open,High,Low,Close\n05/29/2026,18.5,19,18,18\n"
    dispatch = _make_dispatch({
        "fearandgreed": fg_body,
        "VIXPCC": pc_csv,
        "sent_results": aaii_html,
        "VIX_History": vix_csv,
        "VIX9D_History": vix_csv,
        "VIX3M_History": vix_csv,
    })
    block = mt.fetch_market_temperature(
        http_get=dispatch,
        cache_dir=tmp_path / "mt_cache",
        now_fn=lambda: 0.0,
        use_cache=False,
    )

    fragment = schema["$defs"]["market_temperature"]
    # Resolve $refs against the parent schema.
    resolver = jsonschema.RefResolver.from_schema(schema)
    jsonschema.validate(block, fragment, resolver=resolver)


def test_snapshot_schema_version_bumped_to_1_3():
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "ledgers" / "news" / "_schema" / "news_snapshot.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    versions = schema["$defs"]["meta"]["properties"]["schema_version"]["enum"]
    assert "1.3" in versions
    # Top-level allows the new block.
    assert "market_temperature" in schema["properties"]
