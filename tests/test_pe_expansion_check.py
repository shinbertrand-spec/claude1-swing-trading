"""Tests for tools.pe_expansion_check (pure + compute_from_ticker)."""
from __future__ import annotations

import math
from types import SimpleNamespace

import pandas as pd
import pytest

from tools.pe_expansion_check import compute, compute_from_ticker


def test_doubled_pe_triggers_warning():
    e = compute(baseline_pe=18.0, current_pe=38.0)
    assert e.output["pe_expanded"] is True
    assert e.output["warning_late_stage"] is True
    assert math.isclose(e.output["expansion_ratio"], 38.0 / 18.0, rel_tol=1e-9)


def test_no_warning_when_not_doubled():
    e = compute(baseline_pe=18.0, current_pe=25.0)
    assert e.output["pe_expanded"] is False
    assert e.output["warning_late_stage"] is False


def test_custom_threshold():
    e = compute(baseline_pe=10.0, current_pe=15.0, threshold_ratio=1.5)
    assert e.output["pe_expanded"] is True


def test_negative_pe_rejected():
    with pytest.raises(ValueError, match="baseline_pe"):
        compute(baseline_pe=-5.0, current_pe=10.0)
    with pytest.raises(ValueError, match="current_pe"):
        compute(baseline_pe=10.0, current_pe=0.0)


def test_v1_flag_set():
    e = compute(baseline_pe=10.0, current_pe=20.0)
    assert e.output["v1_preliminary_flag"] is True


# ------------------------------------------------- compute_from_ticker


def _fake_eps(ttm_eps):
    return SimpleNamespace(
        ttm_eps=ttm_eps,
        ttm_net_income=ttm_eps * 1e9,
        diluted_shares=1e9,
        ticker="X",
        period_label="X",
        fetched_at="2026-05-25T00:00:00+00:00",
        source="fake:edgar",
    )


def test_compute_from_ticker_below_threshold():
    """AAPL-like: entry $180, current $240 → ratio 1.33 < 2.0 → no warning."""
    fetcher = lambda t: _fake_eps(8.17)  # noqa: E731
    e = compute_from_ticker(
        ticker="AAPL", entry_price=180.0, current_price=240.0,
        _ttm_eps_fetcher=fetcher,
    )
    assert e.output["pe_expanded"] is False
    assert e.output["ticker"] == "AAPL"
    assert e.output["ttm_eps"] == pytest.approx(8.17)
    assert e.output["baseline_pe"] == pytest.approx(180.0 / 8.17, rel=1e-6)
    assert e.output["current_pe"] == pytest.approx(240.0 / 8.17, rel=1e-6)
    assert e.output["expansion_ratio"] == pytest.approx(240.0 / 180.0, rel=1e-6)


def test_compute_from_ticker_above_threshold():
    """Position doubled from entry → fires warning."""
    fetcher = lambda t: _fake_eps(5.0)  # noqa: E731
    e = compute_from_ticker(
        ticker="HOT", entry_price=100.0, current_price=210.0,
        _ttm_eps_fetcher=fetcher,
    )
    assert e.output["pe_expanded"] is True
    assert e.output["warning_late_stage"] is True
    assert e.output["expansion_ratio"] == pytest.approx(2.1, rel=1e-6)


def test_compute_from_ticker_negative_eps_returns_pe_false():
    """Loss-making company → pe_expanded=False with reason, not an exception."""
    fetcher = lambda t: _fake_eps(-2.5)  # noqa: E731
    e = compute_from_ticker(
        ticker="LOSS", entry_price=50.0, current_price=120.0,
        _ttm_eps_fetcher=fetcher,
    )
    assert e.output["pe_expanded"] is False
    assert e.output["ttm_eps"] == pytest.approx(-2.5)
    assert "non-positive" in e.output["reason"].lower()


def test_compute_from_ticker_zero_eps_returns_pe_false():
    fetcher = lambda t: _fake_eps(0.0)  # noqa: E731
    e = compute_from_ticker(
        ticker="ZERO", entry_price=50.0, current_price=120.0,
        _ttm_eps_fetcher=fetcher,
    )
    assert e.output["pe_expanded"] is False
    assert e.output["baseline_pe"] is None


def test_compute_from_ticker_eps_fetch_failure_falls_back_to_false():
    """Network/API failure: tool returns pe_expanded=False with a reason."""
    def boom(_t):
        raise RuntimeError("EDGAR HTTP 503")
    e = compute_from_ticker(
        ticker="NET", entry_price=100.0, current_price=200.0,
        _ttm_eps_fetcher=boom,
    )
    assert e.output["pe_expanded"] is False
    assert "503" in e.output["reason"]
    assert e.output["ttm_eps"] is None


def test_compute_from_ticker_fetches_current_price_when_none():
    fetcher = lambda t: _fake_eps(5.0)  # noqa: E731

    def fake_ohlcv(t, period, interval):
        df = pd.DataFrame({"Close": [100.0, 101.0, 99.0, 102.0, 210.0]})
        return SimpleNamespace(df=df, fetched_at="x", source="x",
                               ticker=t, period=period, interval=interval)

    e = compute_from_ticker(
        ticker="X", entry_price=100.0,
        _ttm_eps_fetcher=fetcher, _ohlcv_fetcher=fake_ohlcv,
    )
    assert e.output["current_price"] == pytest.approx(210.0)
    assert e.output["pe_expanded"] is True


def test_compute_from_ticker_ohlcv_failure_returns_pe_false():
    fetcher = lambda t: _fake_eps(5.0)  # noqa: E731

    def boom(*_a, **_kw):
        raise RuntimeError("YFINANCE_TIMEOUT")

    e = compute_from_ticker(
        ticker="X", entry_price=100.0,
        _ttm_eps_fetcher=fetcher, _ohlcv_fetcher=boom,
    )
    assert e.output["pe_expanded"] is False
    assert "YFINANCE" in e.output["reason"]
    assert e.output["baseline_pe"] is not None  # still computable from entry_price + ttm
    assert e.output["current_pe"] is None


def test_compute_from_ticker_negative_entry_raises():
    fetcher = lambda t: _fake_eps(5.0)  # noqa: E731
    with pytest.raises(ValueError, match="entry_price"):
        compute_from_ticker(
            ticker="X", entry_price=-1.0, current_price=10.0,
            _ttm_eps_fetcher=fetcher,
        )
