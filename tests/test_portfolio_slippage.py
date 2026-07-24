"""Tests for the B4 volume-share slippage mode (portfolio_simulator).

Semantics under test are the zipline-reloaded VolumeShareSlippage port:
per-bar fill cap at volume_limit x bar volume (sub-1-share => miss),
quadratic price impact price_impact x (shares/bar_volume)^2 on
liquidity-demanding transactions, remainder cancels (DAY orders).
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from tools.backtest import portfolio_simulator as ps
from tools.backtest.setup_replay import TradeSignal


def _df(prices, volume=1_000_000.0, start="2024-01-01", volumes=None) -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=len(prices))
    p = pd.Series(prices, index=idx, dtype=float)
    v = pd.Series(volumes, index=idx, dtype=float) if volumes is not None else volume
    return pd.DataFrame(
        {"Open": p, "High": p * 1.001, "Low": p * 0.999, "Close": p, "Volume": v},
        index=idx,
    )


def _sig(ticker, kind, entry_i, fill_i, idx, stop, target=None, max_hold=20):
    return TradeSignal(
        ticker=ticker, setup_type=kind, setup_grade="B",
        entry_date=idx[entry_i].date(), fill_date=idx[fill_i].date(),
        entry_price=0.0, stop_price=stop, target_price=target,
        max_hold_days=max_hold, atr_at_signal=1.0, notes={},
    )


NO_COST = ps.PortfolioConfig(apply_costs=False)


def test_entry_capped_at_volume_limit_with_quadratic_impact():
    # bar volume 500 -> max_fill = 50; ADV $50k -> liq floor 0.2 ->
    # target $10k -> desired 99 shares at open 101 -> capped to 50.
    # vshare = 50/500 = 0.1 -> impacted = 101 * (1 + 0.1*0.01) = 101.101
    prices = [100.0] * 70 + [100.0, 101.0, 102.0, 103.0, 104.0]
    df = _df(prices, volume=500.0)
    sig = _sig("AAA", "ts_momentum", 70, 71, df.index, stop=90.0, max_hold=2)
    res = ps.simulate([sig], {"AAA": df}, NO_COST, volume_share_slippage=True)
    assert res.n_filled == 1
    assert res.n_volume_capped == 1
    t = res.trades[0]
    assert t.shares == 50
    assert t.entry_fill_price == pytest.approx(101.0 * 1.001, rel=1e-9)


def test_entry_blocked_when_cap_below_one_share():
    prices = [100.0] * 70 + [100.0, 101.0, 102.0, 103.0]
    df = _df(prices, volume=5.0)  # max_fill = int(0.5) = 0
    sig = _sig("AAA", "ts_momentum", 70, 71, df.index, stop=90.0, max_hold=2)
    res = ps.simulate([sig], {"AAA": df}, NO_COST, volume_share_slippage=True)
    assert res.n_filled == 0
    assert res.n_volume_blocked == 1
    assert res.n_missed == 1


def test_mode_off_is_bit_identical_to_baseline():
    prices = [100.0] * 70 + [100.0, 101.0, 102.0, 103.0, 104.0]
    df = _df(prices, volume=500.0)
    sig = _sig("AAA", "ts_momentum", 70, 71, df.index, stop=90.0, max_hold=2)
    base = ps.simulate([sig], {"AAA": df}, NO_COST)
    off = ps.simulate([sig], {"AAA": df}, NO_COST, volume_share_slippage=False)
    assert base.n_volume_capped == off.n_volume_capped == 0
    assert base.trades[0].shares == off.trades[0].shares
    assert base.trades[0].entry_fill_price == off.trades[0].entry_fill_price
    assert base.equity_curve.equals(off.equity_curve)


def test_marketable_stop_exit_pays_quadratic_impact():
    # entry bar has deep volume (no cap); the stop-hit bar is THIN, so the
    # full-size exit pays impact: exit = stop * (1 - 0.1 * (shares/vol)^2)
    prices = [100.0] * 70 + [100.0, 101.0, 95.0, 89.0]
    volumes = [100_000.0] * 72 + [1_000.0, 1_000.0]
    df = _df(prices, volumes=volumes)
    sig = _sig("AAA", "ts_momentum", 70, 71, df.index, stop=90.0, max_hold=20)
    res = ps.simulate([sig], {"AAA": df}, NO_COST, volume_share_slippage=True)
    assert res.n_filled == 1
    t = res.trades[0]
    assert t.exit_reason in ("stop_hit", "gap_through_stop")
    stop_ref = 90.0 if t.exit_reason == "stop_hit" else 89.0
    vshare = t.shares / 1_000.0
    assert t.exit_fill_price == pytest.approx(
        stop_ref * (1.0 - 0.1 * vshare * vshare), rel=1e-9,
    )
    assert t.exit_fill_price < stop_ref


def test_passive_target_exit_pays_no_impact():
    prices = [100.0] * 70 + [100.0, 101.0, 105.0, 106.0]
    volumes = [100_000.0] * 72 + [100.0, 100.0]  # thin, but passive limit
    df = _df(prices, volumes=volumes)
    sig = _sig("AAA", "ts_momentum", 70, 71, df.index, stop=90.0,
               target=105.0, max_hold=20)
    res = ps.simulate([sig], {"AAA": df}, NO_COST, volume_share_slippage=True)
    assert res.n_filled == 1
    t = res.trades[0]
    assert t.exit_reason == "target_hit"
    assert t.exit_fill_price == pytest.approx(105.0)


def test_parameter_validation():
    df = _df([100.0] * 75)
    sig = _sig("AAA", "ts_momentum", 70, 71, df.index, stop=90.0)
    with pytest.raises(ValueError, match="volume_limit"):
        ps.simulate([sig], {"AAA": df}, NO_COST,
                    volume_share_slippage=True, volume_limit=0.0)
    with pytest.raises(ValueError, match="price_impact"):
        ps.simulate([sig], {"AAA": df}, NO_COST,
                    volume_share_slippage=True, price_impact=-0.1)


def test_result_flags_carry_mode():
    prices = [100.0] * 70 + [100.0, 101.0, 102.0, 103.0]
    df = _df(prices)
    sig = _sig("AAA", "ts_momentum", 70, 71, df.index, stop=90.0, max_hold=2)
    res = ps.simulate([sig], {"AAA": df}, NO_COST, volume_share_slippage=True)
    assert res.volume_share_slippage is True
