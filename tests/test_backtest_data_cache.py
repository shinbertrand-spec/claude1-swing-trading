"""Tests for tools.backtest.data_cache — pure fs operations only."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from tools.backtest import data_cache


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch):
    """Redirect the cache dir to a tmp_path for the test."""
    monkeypatch.setattr(data_cache, "CACHE_DIR", tmp_path / "cache")
    yield tmp_path


def _seed_cache(cache_dir: Path, ticker: str, n_bars: int = 5) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    idx = pd.date_range("2024-01-02", periods=n_bars, freq="B")
    df = pd.DataFrame(
        {
            "Open": [100.0] * n_bars,
            "High": [101.0] * n_bars,
            "Low": [99.0] * n_bars,
            "Close": [100.5] * n_bars,
            "Volume": [1_000_000] * n_bars,
        },
        index=idx,
    )
    df.to_parquet(cache_dir / f"{ticker.upper()}.parquet")
    (cache_dir / f"{ticker.upper()}.meta.txt").write_text(
        f"fetched_at=2026-05-18T10:00:00+00:00\nsource=yfinance:{ticker}\nrows={n_bars}\n",
        encoding="utf-8",
    )


def test_load_reads_cached_dataframe(isolated_cache: Path):
    _seed_cache(isolated_cache / "cache", "AAPL")
    df = data_cache.load("AAPL")
    assert len(df) == 5
    assert "Close" in df.columns


def test_load_raises_for_missing(isolated_cache: Path):
    with pytest.raises(FileNotFoundError, match="No cache for ZZZ"):
        data_cache.load("ZZZ")


def test_info_returns_metadata(isolated_cache: Path):
    _seed_cache(isolated_cache / "cache", "MSFT", n_bars=10)
    e = data_cache.info("MSFT")
    assert e is not None
    assert e.ticker == "MSFT"
    assert e.rows == 10
    assert e.fetched_at.startswith("2026-05-18")


def test_info_returns_none_for_missing(isolated_cache: Path):
    assert data_cache.info("ZZZ") is None


def test_clear_removes_files(isolated_cache: Path):
    _seed_cache(isolated_cache / "cache", "NVDA")
    assert data_cache.clear("NVDA") is True
    assert data_cache.info("NVDA") is None
    # Idempotent.
    assert data_cache.clear("NVDA") is False


def test_cached_load_skips_fetch_when_present(isolated_cache: Path):
    _seed_cache(isolated_cache / "cache", "GOOGL")
    # force_refetch=False — should NOT attempt network. If it did, this
    # would error in the test env (no network mock).
    e = data_cache.fetch("GOOGL", force_refetch=False)
    assert e.rows == 5
    assert e.ticker == "GOOGL"
