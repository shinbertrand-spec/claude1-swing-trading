"""Tests for tools.auto_paper.benchmarks — A2 baseline-honesty stats.

Deterministic synthetic price series; hand-computed expectations. No network
— compute_baselines is exercised via an injected price_loader.
"""
from __future__ import annotations

import datetime as _dt

import pandas as pd

from tools.auto_paper import benchmarks as bm


def _series(values, start="2026-01-05"):
    idx = pd.date_range(start, periods=len(values), freq="B")
    return pd.Series([float(v) for v in values], index=idx)


# ---------------------------------------------------------------- buy & hold


def test_buy_and_hold_total_return_and_dd():
    # 100 -> 110 -> 99 -> 121: total +21%, max DD = 99/110 - 1 = -10%.
    s = _series([100, 110, 99, 121])
    stats = bm.buy_and_hold("bh", s)
    assert stats.total_return_pct == 21.0
    assert stats.max_drawdown_pct == -10.0
    assert stats.n_days == 4
    assert stats.sharpe_annualised is not None


def test_buy_and_hold_insufficient_data():
    stats = bm.buy_and_hold("bh", _series([100]))
    assert stats.total_return_pct is None
    assert "insufficient" in stats.note


def test_flat_series_sharpe_none_not_crash():
    stats = bm.buy_and_hold("bh", _series([100] * 10))
    assert stats.total_return_pct == 0.0
    assert stats.sharpe_annualised is None  # zero variance
    assert stats.max_drawdown_pct == 0.0


# ---------------------------------------------------------------- equal weight


def test_equal_weight_averages_normalised_paths():
    # A doubles, B halves -> equal-weight ends at (2.0 + 0.5) / 2 = 1.25.
    a = _series([10, 20])
    b = _series([40, 20])
    stats = bm.equal_weight_buy_and_hold("ew", {"A": a, "B": b})
    assert stats.total_return_pct == 25.0


def test_equal_weight_empty():
    stats = bm.equal_weight_buy_and_hold("ew", {})
    assert stats.total_return_pct is None


# ---------------------------------------------------------------- 12-1 momentum


def test_ts_momentum_needs_lookback():
    stats = bm.ts_momentum_12_1("mom", _series([100, 101, 102]))
    assert stats.total_return_pct is None
    assert "lookback" in stats.note


def test_ts_momentum_long_in_uptrend():
    # Monotonic uptrend: signal always positive once defined -> strategy
    # tracks the underlying (positive return, invested every scored bar).
    n = 300
    s = _series([100 * (1.005 ** i) for i in range(n)])
    stats = bm.ts_momentum_12_1("mom", s)
    assert stats.total_return_pct is not None
    assert stats.total_return_pct > 0


def test_ts_momentum_flat_in_downtrend():
    # Monotonic downtrend: 12-1 signal negative once defined -> in cash,
    # scored-window return ~0 and no drawdown.
    n = 300
    s = _series([100 * (0.997 ** i) for i in range(n)])
    stats = bm.ts_momentum_12_1("mom", s)
    assert stats.total_return_pct == 0.0
    assert stats.max_drawdown_pct == 0.0


# ---------------------------------------------------------------- compute + render


def _fake_loader(ticker, start, end):
    n = (end - start).days + 1
    base = {"SPY": 100.0, "AAA": 50.0, "BBB": 20.0}.get(ticker, 10.0)
    idx = pd.date_range(start, periods=max(n, 2), freq="D")
    return pd.Series([base * (1.001 ** i) for i in range(len(idx))], index=idx)


def test_compute_baselines_all_three_legs():
    out = bm.compute_baselines(
        window_start=_dt.date(2026, 5, 1),
        window_end=_dt.date(2026, 6, 30),
        traded_tickers=["AAA", "BBB", "AAA"],
        price_loader=_fake_loader,
    )
    names = [b["name"] for b in out["baselines"]]
    assert names == [
        "spy_buy_hold", "equal_weight_traded_buy_hold", "spy_ts_momentum_12_1",
    ]
    assert out["window_start"] == "2026-05-01"
    spy = out["baselines"][0]
    assert spy["total_return_pct"] is not None


def test_compute_baselines_degrades_per_leg_never_raises():
    def _broken(ticker, start, end):
        raise RuntimeError("no data source")

    out = bm.compute_baselines(
        window_start=_dt.date(2026, 5, 1),
        window_end=_dt.date(2026, 6, 30),
        traded_tickers=["AAA"],
        price_loader=_broken,
    )
    assert len(out["baselines"]) == 3
    assert all("error" in b["note"] for b in out["baselines"])


def test_render_markdown_table():
    out = bm.compute_baselines(
        window_start=_dt.date(2026, 5, 1),
        window_end=_dt.date(2026, 6, 30),
        traded_tickers=["AAA"],
        price_loader=_fake_loader,
    )
    md = bm.render_baselines_markdown(out)
    assert "| spy_buy_hold |" in md
    assert "2026-05-01 -> 2026-06-30" in md
    assert bm.render_baselines_markdown(None) == ""
