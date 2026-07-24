"""Tests for tools.asof — A3 as-of data-layer contract.

Covers the shared helper (filter/parse/latest_knowable), the LIVE-ONLY
adapter gate, and the two adapter fixes shipped with the policy:
``data_cache.load(end=)`` and ``earnings_calendar`` as-of anchoring
(the latter's parser test lives in tests/test_earnings_calendar.py).
"""
from __future__ import annotations

import datetime as _dt

import pandas as pd
import pytest

from tools import asof
from tools.backtest import data_cache


# ---------------------------------------------------------------- helpers


def test_parse_as_of_shapes():
    d = _dt.date(2026, 6, 1)
    assert asof.parse_as_of("2026-06-01") == d
    assert asof.parse_as_of("2026-06-01T14:30:00+00:00") == d
    assert asof.parse_as_of(d) == d
    assert asof.parse_as_of(_dt.datetime(2026, 6, 1, 9, 30)) == d


def test_filter_as_of_excludes_future_and_undated():
    items = [
        {"id": "past", "published": "2026-05-30"},
        {"id": "same-day", "published": "2026-06-01"},
        {"id": "future", "published": "2026-06-02"},
        {"id": "undated", "published": None},
        {"id": "garbage", "published": "not-a-date"},
    ]
    kept = asof.filter_as_of(items, lambda x: x["published"], "2026-06-01")
    assert [x["id"] for x in kept] == ["past", "same-day"]


def test_latest_knowable():
    dates = ["2026-01-15", "2026-03-10", "2026-06-30"]
    assert asof.latest_knowable(dates, "2026-04-01") == _dt.date(2026, 3, 10)
    assert asof.latest_knowable(dates, "2025-12-31") is None


# ---------------------------------------------------------------- live-only gate


def test_assert_backtest_safe_blocks_live_only():
    with pytest.raises(RuntimeError, match="LIVE-ONLY"):
        asof.assert_backtest_safe("tools.auto_paper.screener")


def test_assert_backtest_safe_passes_pit_adapters():
    asof.assert_backtest_safe("tools.fundamentals.pit_fundamentals")
    asof.assert_backtest_safe("tools.backtest.data_cache")


def test_live_only_registry_names_pit_alternative_for_edgar_eps():
    reason = asof.LIVE_ONLY_ADAPTERS["tools.fundamentals.edgar_eps"]
    assert "pit_fundamentals" in reason


# ---------------------------------------------------------------- data_cache.load(end=)


def test_data_cache_load_end_trims_future_bars(tmp_path, monkeypatch):
    monkeypatch.setattr(data_cache, "CACHE_DIR", tmp_path)
    idx = pd.date_range("2026-05-28", periods=5, freq="D", tz="America/New_York")
    df = pd.DataFrame({"Close": [1.0, 2.0, 3.0, 4.0, 5.0]}, index=idx)
    df.to_parquet(tmp_path / "TEST.parquet")

    full = data_cache.load("TEST")
    assert len(full) == 5

    trimmed = data_cache.load("TEST", end="2026-05-30")
    assert len(trimmed) == 3
    assert trimmed["Close"].tolist() == [1.0, 2.0, 3.0]

    trimmed_date = data_cache.load("TEST", end=_dt.date(2026, 5, 28))
    assert len(trimmed_date) == 1
