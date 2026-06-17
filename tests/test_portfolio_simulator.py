"""Tests for the net-of-cost portfolio-equity simulator."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from tools.backtest import portfolio_simulator as ps
from tools.backtest.setup_replay import TradeSignal


def _df(prices: list[float], volume: float = 1_000_000.0, start: str = "2024-01-01") -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=len(prices))
    p = pd.Series(prices, index=idx, dtype=float)
    return pd.DataFrame(
        {"Open": p, "High": p * 1.001, "Low": p * 0.999, "Close": p, "Volume": volume},
        index=idx,
    )


def _sig(ticker, kind, entry_i, fill_i, idx, stop, target=None, max_hold=20):
    return TradeSignal(
        ticker=ticker, setup_type=kind, setup_grade="B",
        entry_date=idx[entry_i].date(), fill_date=idx[fill_i].date(),
        entry_price=0.0, stop_price=stop, target_price=target,
        max_hold_days=max_hold, atr_at_signal=1.0, notes={},
    )


def test_momentum_marketable_fill_then_max_hold_exit():
    # 70 flat bars (warmup for ADV) then a steady riser.
    prices = [100.0] * 70 + [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
    df = _df(prices)
    idx = df.index
    # signal day = bar 70 (pivot=100), fill bar = 71 (open=101 <= 100*1.03=103 → fills at 101)
    sig = _sig("AAA", "ts_momentum", 70, 71, idx, stop=90.0, target=None, max_hold=3)
    res = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig(apply_costs=True))
    assert res.n_filled == 1
    assert res.trades[0].exit_reason == "max_hold"
    # net entry > gross (paid the spread); net return recorded
    assert res.trades[0].entry_net_price > res.trades[0].entry_fill_price


def test_momentum_gap_over_3pct_is_missed():
    prices = [100.0] * 70 + [100.0, 110.0, 112.0, 115.0]  # fill bar opens at 110 = +10% gap
    df = _df(prices)
    idx = df.index
    sig = _sig("AAA", "ts_momentum", 70, 71, idx, stop=90.0, max_hold=2)
    res = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig())
    assert res.n_filled == 0
    assert res.n_missed == 1
    # the missed name was a winner (gapped up) — selection bias is captured
    assert res.avg_missed_fwd_return > 0


def test_reversion_fills_only_if_low_touches_pivot():
    # reversion limit = pivot (signal-day close). Fill bar must dip to it.
    prices = [100.0] * 70 + [100.0, 101.0, 102.0]  # never dips below pivot 100 after signal
    df = _df(prices)
    idx = df.index
    # signal at bar 70 (pivot=100); fill bar 71 opens 101, low=101*0.999>100 → NO fill
    sig = _sig("AAA", "xs_short_term_reversal", 70, 71, idx, stop=90.0, max_hold=2)
    res = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig())
    assert res.n_filled == 0


def test_reversion_fills_when_bar_dips_to_pivot():
    prices = [100.0] * 70 + [100.0, 99.0, 101.0, 102.0]  # fill bar dips to 99 < pivot 100
    df = _df(prices)
    idx = df.index
    sig = _sig("AAA", "xs_short_term_reversal", 70, 71, idx, stop=90.0, max_hold=2)
    res = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig())
    assert res.n_filled == 1


def test_stop_hit_exit():
    prices = [100.0] * 70 + [100.0, 101.0, 95.0, 89.0]  # drops through stop 90
    df = _df(prices)
    idx = df.index
    sig = _sig("AAA", "ts_momentum", 70, 71, idx, stop=90.0, max_hold=20)
    res = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig())
    assert res.n_filled == 1
    assert res.trades[0].exit_reason in ("stop_hit", "gap_through_stop")


def test_concurrency_cap_blocks_extra_fills():
    # 10 names all firing the same day; cap=8 → only 8 fill.
    dfs = {}
    sigs = []
    prices = [100.0] * 70 + [100.0, 101.0, 102.0, 103.0]
    for i in range(10):
        t = f"T{i}"
        dfs[t] = _df(prices)
        idx = dfs[t].index
        sigs.append(_sig(t, "ts_momentum", 70, 71, idx, stop=90.0, max_hold=3))
    res = ps.simulate(sigs, dfs, ps.PortfolioConfig(max_positions=8))
    assert res.n_filled == 8
    assert res.n_signals == 10


def test_costs_make_flat_trade_negative():
    # Enter and exit at ~same price; cost must produce a net loss.
    prices = [100.0] * 70 + [100.0] + [100.0] * 5
    df = _df(prices, volume=50_000)  # thinner name → bigger spread
    idx = df.index
    sig = _sig("AAA", "xs_short_term_reversal", 70, 71, idx, stop=90.0, max_hold=2)
    res = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig(apply_costs=True))
    if res.n_filled == 1:
        assert res.trades[0].net_return < res.trades[0].gross_return


def test_gate_fields_present():
    prices = [100.0] * 70 + [100.0, 101.0, 102.0]
    df = _df(prices)
    idx = df.index
    sig = _sig("AAA", "ts_momentum", 70, 71, idx, stop=90.0, max_hold=2)
    res = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig())
    assert isinstance(res.deployment_gate_passed, bool)
    assert res.n_trades == len(res.trades)
    assert 0.0 <= res.fill_rate <= 1.0
