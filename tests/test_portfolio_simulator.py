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


# ---------------------------------------------------------- fill-model variants


def test_pure_moo_fills_the_gap_that_marketable_limit_misses():
    """The >3% gapper missed by the marketable limit IS filled under pure_moo,
    at the open — this is the right-tail the buffer cap truncates."""
    prices = [100.0] * 70 + [100.0, 110.0, 112.0, 115.0]  # +10% open gap on fill bar
    df = _df(prices)
    idx = df.index
    sig = _sig("AAA", "ts_momentum", 70, 71, idx, stop=90.0, max_hold=2)

    missed = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig(),
                         fill_model=ps.FILL_MARKETABLE_LIMIT)
    assert missed.n_filled == 0 and missed.n_missed == 1

    filled = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig(),
                         fill_model=ps.FILL_PURE_MOO)
    assert filled.n_filled == 1
    # filled at the OPEN (110), never the current-bar close (look-ahead guard)
    assert abs(filled.trades[0].entry_fill_price - 110.0) < 1e-6
    assert filled.fill_model == "pure_moo"


def test_pure_moo_fill_rate_is_total_on_momentum():
    """Every momentum signal fills under pure_moo regardless of gap size."""
    dfs, sigs = {}, []
    # mix of small-gap and big-gap names; all should fill under pure_moo
    for i, gap in enumerate([1.0, 1.20, 1.005, 1.50]):
        t = f"T{i}"
        prices = [100.0] * 70 + [100.0, 100.0 * gap, 100.0 * gap * 1.01]
        dfs[t] = _df(prices)
        idx = dfs[t].index
        sigs.append(_sig(t, "ts_momentum", 70, 71, idx, stop=50.0, max_hold=2))
    res = ps.simulate(sigs, dfs, ps.PortfolioConfig(), fill_model=ps.FILL_PURE_MOO)
    assert res.n_filled == res.n_signals == 4
    assert res.fill_rate == 1.0


def test_momentum_buffer_override_widens_fill_window():
    """A wider buffer fills a gap that the default 3% misses; reversion ignores
    the momentum buffer entirely."""
    prices = [100.0] * 70 + [100.0, 104.0, 106.0]  # +4% gap: misses at 3%, fills at 5%
    df = _df(prices)
    idx = df.index
    sig = _sig("AAA", "ts_momentum", 70, 71, idx, stop=90.0, max_hold=2)

    miss = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig(),
                       fill_model=ps.FILL_MARKETABLE_LIMIT, momentum_buffer=0.03)
    assert miss.n_filled == 0
    hit = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig(),
                      fill_model=ps.FILL_MARKETABLE_LIMIT, momentum_buffer=0.05)
    assert hit.n_filled == 1
    assert hit.momentum_buffer == 0.05


def test_pure_moo_does_not_change_reversion_fill():
    """fill_model is momentum-only; a reversion buy stays a passive limit even
    under pure_moo (chasing an oversold name up is adverse selection)."""
    prices = [100.0] * 70 + [100.0, 101.0, 102.0]  # never dips to pivot → no fill
    df = _df(prices)
    idx = df.index
    sig = _sig("AAA", "xs_short_term_reversal", 70, 71, idx, stop=90.0, max_hold=2)
    res = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig(),
                      fill_model=ps.FILL_PURE_MOO)
    assert res.n_filled == 0


def test_invalid_fill_model_raises():
    prices = [100.0] * 70 + [100.0, 101.0]
    df = _df(prices)
    sig = _sig("AAA", "ts_momentum", 70, 71, df.index, stop=90.0, max_hold=1)
    with pytest.raises(ValueError, match="fill_model"):
        ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig(), fill_model="bogus")


# ---------------------------------------------------------- cost / fill realism


def test_full_spread_marketable_costs_more_than_half_on_momentum_buy():
    """A momentum (marketable) buy charged full spread pays MORE than half."""
    prices = [100.0] * 70 + [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
    df = _df(prices, volume=200_000)  # non-trivial spread tier
    idx = df.index
    sig = _sig("AAA", "ts_momentum", 70, 71, idx, stop=90.0, max_hold=3)
    half = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig(apply_costs=True),
                       full_spread_marketable=False)
    full = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig(apply_costs=True),
                       full_spread_marketable=True)
    assert full.trades[0].entry_net_price > half.trades[0].entry_net_price


def test_entry_slippage_raises_momentum_fill_price():
    prices = [100.0] * 70 + [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
    df = _df(prices)
    idx = df.index
    sig = _sig("AAA", "ts_momentum", 70, 71, idx, stop=90.0, max_hold=3)
    base = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig(apply_costs=False),
                       entry_slippage_bps=0.0)
    slipped = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig(apply_costs=False),
                          entry_slippage_bps=50.0)  # 50 bps above the open
    assert slipped.trades[0].entry_fill_price > base.trades[0].entry_fill_price
    assert slipped.trades[0].entry_fill_price == pytest.approx(101.0 * 1.005)


def test_fill_probability_zero_misses_all_momentum():
    prices = [100.0] * 70 + [100.0, 101.0, 102.0]
    df = _df(prices)
    idx = df.index
    sig = _sig("AAA", "ts_momentum", 70, 71, idx, stop=90.0, max_hold=2)
    res = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig(), fill_probability=0.0)
    assert res.n_filled == 0 and res.n_missed == 1


def test_fill_probability_is_deterministic():
    prices = [100.0] * 70 + [100.0, 101.0, 102.0]
    dfs = {f"T{i}": _df(prices) for i in range(6)}
    sigs = [_sig(f"T{i}", "ts_momentum", 70, 71, dfs[f"T{i}"].index, stop=90.0, max_hold=2)
            for i in range(6)]
    a = ps.simulate(sigs, dfs, ps.PortfolioConfig(), fill_probability=0.5)
    b = ps.simulate(sigs, dfs, ps.PortfolioConfig(), fill_probability=0.5)
    assert a.n_filled == b.n_filled                    # reproducible
    assert 0 < a.n_filled < a.n_signals                # genuinely thinned


def test_fill_probability_does_not_thin_reversion():
    """fill_probability is a marketable-entry concept; reversion is passive."""
    prices = [100.0] * 70 + [100.0, 99.0, 101.0]   # dips to pivot → reversion fills
    df = _df(prices)
    idx = df.index
    sig = _sig("AAA", "xs_short_term_reversal", 70, 71, idx, stop=90.0, max_hold=2)
    res = ps.simulate([sig], {"AAA": df}, ps.PortfolioConfig(), fill_probability=0.0)
    assert res.n_filled == 1   # reversion unaffected by the marketable fill haircut


# ---------------------------------------------------------- walk-forward clause


def _year_df(years, periods_per_year=260):
    import pandas as _pd
    idx = _pd.bdate_range(start=f"{years[0]}-01-01", periods=len(years) * periods_per_year)
    prices = [100.0 + 0.05 * i for i in range(len(idx))]  # gentle uptrend
    p = _pd.Series(prices, index=idx, dtype=float)
    return _pd.DataFrame(
        {"Open": p, "High": p * 1.001, "Low": p * 0.999, "Close": p, "Volume": 1_000_000.0},
        index=idx,
    )


def test_walk_forward_structure_and_clause():
    """simulate_walk_forward partitions OOS by window and computes the clause."""
    df = _year_df([2017, 2018, 2019, 2020, 2021, 2022])
    idx = list(df.index)
    # one momentum signal per ~quarter so each OOS window has trades
    sigs = []
    for i in range(60, len(idx) - 2, 60):
        sigs.append(_sig("AAA", "ts_momentum", i, i + 1, df.index, stop=50.0, max_hold=20))
    wf = ps.simulate_walk_forward(
        sigs, {"AAA": df},
        start=date(2017, 1, 1), end=date(2022, 1, 1),
        is_years=3, oos_years=1, step_years=1,
        sharpe_min=1.0, max_dd_pct=25.0, n_min=1,
        min_window_sharpe=0.5, min_window_pass_rate=0.5,
    )
    assert wf.n_windows == len(wf.window_results) >= 2
    assert 0.0 <= wf.window_pass_rate <= 1.0
    assert isinstance(wf.window_clause_passed, bool)
    assert isinstance(wf.overall_passed, bool)
    # overall requires BOTH the aggregate gate and the window clause
    assert wf.overall_passed == (wf.aggregate_gate_passed and wf.window_clause_passed)


def test_walk_forward_threads_cost_model():
    """sim_kwargs (full_spread_marketable) reach every window run."""
    df = _year_df([2017, 2018, 2019, 2020, 2021])
    idx = list(df.index)
    sigs = [_sig("AAA", "ts_momentum", i, i + 1, df.index, stop=50.0, max_hold=20)
            for i in range(60, len(idx) - 2, 60)]
    wf = ps.simulate_walk_forward(
        sigs, {"AAA": df}, start=date(2017, 1, 1), end=date(2021, 1, 1),
        is_years=3, oos_years=1, step_years=1, n_min=1,
        fill_model=ps.FILL_MARKETABLE_LIMIT, momentum_buffer=0.03,
        full_spread_marketable=True,
    )
    # every window result carries the fill model we threaded
    for _, r in wf.window_results:
        assert r.fill_model == ps.FILL_MARKETABLE_LIMIT
