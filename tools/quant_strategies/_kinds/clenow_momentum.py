"""Clenow Stocks-on-the-Move momentum strategy.

Per Andreas Clenow, *Stocks on the Move* (2015). Multi-week momentum:

* Regime filter: only enter longs when benchmark (SPY) closes above its
  ``regime_filter_period``-day SMA (default 200).
* Per-name signal: ``momentum_lookback_days``-bar exponential regression
  slope, annualised, multiplied by R^2 (the *regression score*). Higher
  = stronger, smoother uptrend.
* Cross-sectional ranking: each rebalance date, rank the in-universe
  names by regression score; the top ``top_k`` qualify for entry.
* Hold semantics: position exits when ticker drops out of top-K OR hits
  the ATR-based stop OR ``max_hold_days`` elapses (the v1 compatibility
  shim — see "v1 caveat" below).
* Sizing: ATR-based (per-position risk = ``atr_risk_pct`` of equity);
  the simulator handles this implicitly via R-multiple semantics.

This kind's contribution to the swing-trading framework: a **portfolio
strategy** rather than a per-name pattern detector. Most setups in
:mod:`tools.backtest.setup_replay` are "this chart shows X, enter now";
Clenow is "the universe as a whole shows X is the strongest, hold it
until it isn't." Validates the quant-strategist subagent's value-add.

## v1 caveat — TradeSignal compatibility shim

Faithful Clenow rebalancing has positions held continuously across
rebalance dates (e.g. AAPL stays in top-K weekly for 8 weeks → one
trade lasting 8 weeks). The existing :class:`tools.backtest.simulator`
contract is one-entry-one-exit per :class:`TradeSignal`. v1 approximates
this by emitting one TradeSignal per rebalance date when ticker is in
top-K, with ``max_hold_days = rebalance_period_days``. Re-entry happens
on the next rebalance if ticker still qualifies. Trade count is inflated
by ~ ``actual_hold_weeks`` vs faithful Clenow.

The faithful approach (continuous holds, dynamic exits) needs a
portfolio simulator with concurrent positions + cash management +
rebalance scheduling — Phase 5.d work. v1 ships the shim to validate
the quant-strategist architecture end-to-end.
"""
from __future__ import annotations

from typing import NamedTuple

import numpy as np
import pandas as pd

from ...backtest.setup_replay import TradeSignal


KIND = "clenow_momentum"


class CrossSectionalState(NamedTuple):
    """Pre-computed cross-sectional ranking state.

    ``ranks_by_date`` maps each rebalance date to a set of tickers in
    the top-K on that date. ``benchmark_regime_ok`` maps each date to
    whether the regime filter passed (benchmark above SMA).
    """
    ranks_by_date: dict[pd.Timestamp, set[str]]
    benchmark_regime_ok: dict[pd.Timestamp, bool]
    rebalance_dates: list[pd.Timestamp]


def _annualised_log_slope_r2(closes: pd.Series, lookback: int) -> float:
    """Compute the Clenow regression score: annualised log-slope * R^2.

    Higher score = stronger, smoother uptrend. Negative slope yields
    a negative score regardless of R^2.
    """
    if len(closes) < lookback:
        return float("-inf")
    window = closes.iloc[-lookback:].to_numpy()
    if np.any(window <= 0) or np.any(np.isnan(window)):
        return float("-inf")
    log_p = np.log(window)
    x = np.arange(len(log_p), dtype=float)
    # Linear regression on log prices.
    slope, intercept = np.polyfit(x, log_p, 1)
    fitted = slope * x + intercept
    ss_res = np.sum((log_p - fitted) ** 2)
    ss_tot = np.sum((log_p - log_p.mean()) ** 2)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    # Annualise: 252 trading days. slope is per-bar log-return.
    annualised = (np.exp(slope * 252) - 1.0)
    return float(annualised * max(r2, 0.0))


def precompute(
    universe_dfs: dict[str, pd.DataFrame],
    params: dict,
) -> CrossSectionalState:
    """Build the cross-sectional rebalance state.

    Args:
        universe_dfs: maps ticker (including the benchmark) to its OHLCV
            DataFrame indexed by date.
        params: strategy params; must include ``benchmark``,
            ``regime_filter_period``, ``momentum_lookback_days``,
            ``rebalance_period_days``, ``top_k``.

    Returns:
        :class:`CrossSectionalState` for use in :func:`replay`.
    """
    benchmark = params["benchmark"]
    regime_period = int(params["regime_filter_period"])
    lookback = int(params["momentum_lookback_days"])
    rebalance_period = int(params["rebalance_period_days"])
    top_k = int(params["top_k"])

    if benchmark not in universe_dfs:
        raise ValueError(
            f"benchmark {benchmark!r} not in universe_dfs; add it to the spec's universe.tickers"
        )

    bench_df = universe_dfs[benchmark]
    bench_close = bench_df["Close"]
    bench_sma = bench_close.rolling(window=regime_period, min_periods=regime_period).mean()
    benchmark_regime_ok: dict[pd.Timestamp, bool] = {
        d: bool(bench_close.loc[d] > bench_sma.loc[d])
        for d in bench_close.index
        if not pd.isna(bench_sma.loc[d])
    }

    # Find rebalance dates: every Nth bar of the benchmark's index,
    # starting once we have enough history for the lookback.
    bench_dates = list(bench_close.index)
    if len(bench_dates) < max(lookback, regime_period) + 1:
        return CrossSectionalState({}, benchmark_regime_ok, [])
    start_idx = max(lookback, regime_period)
    rebalance_dates = [bench_dates[i] for i in range(start_idx, len(bench_dates), rebalance_period)]

    # On each rebalance date, rank the universe by regression score and
    # take the top-K. Exclude the benchmark itself from ranking.
    candidate_tickers = [t for t in universe_dfs if t != benchmark]
    ranks_by_date: dict[pd.Timestamp, set[str]] = {}
    for d in rebalance_dates:
        scores: list[tuple[float, str]] = []
        for t in candidate_tickers:
            tdf = universe_dfs[t]
            if d not in tdf.index:
                continue
            closes_through = tdf["Close"].loc[:d]
            score = _annualised_log_slope_r2(closes_through, lookback)
            if np.isfinite(score):
                scores.append((score, t))
        scores.sort(reverse=True)
        ranks_by_date[d] = {t for _, t in scores[:top_k]}

    return CrossSectionalState(ranks_by_date, benchmark_regime_ok, rebalance_dates)


def replay(
    df: pd.DataFrame,
    ticker: str,
    params: dict,
    state: CrossSectionalState,
) -> list[TradeSignal]:
    """Per-ticker signal emission given the precomputed cross-sectional state.

    Walks each rebalance date; emits a TradeSignal if (a) the ticker
    is in the top-K on that date AND (b) the regime filter passed AND
    (c) the next-bar Open is available for fill.
    """
    if not state.rebalance_dates:
        return []

    atr_period = int(params.get("atr_period", 20))
    atr_stop_multiple = float(params.get("atr_stop_multiple", 3.0))
    rebalance_period = int(params["rebalance_period_days"])
    max_hold_days = int(params.get("max_hold_days", rebalance_period))
    target_r_multiple = params.get("target_r_multiple")
    if target_r_multiple is not None:
        target_r_multiple = float(target_r_multiple)

    if "Open" not in df.columns or "Close" not in df.columns:
        raise ValueError(f"{ticker}: df missing Open/Close columns")

    signals: list[TradeSignal] = []
    df_index = list(df.index)
    for rebal_date in state.rebalance_dates:
        if ticker not in state.ranks_by_date.get(rebal_date, set()):
            continue
        if not state.benchmark_regime_ok.get(rebal_date, False):
            continue
        if rebal_date not in df.index:
            continue

        # Need a next bar to fill on.
        try:
            i = df_index.index(rebal_date)
        except ValueError:
            continue
        if i + 1 >= len(df_index):
            continue
        next_bar = df.iloc[i + 1]
        entry_price = float(next_bar["Open"])
        if entry_price <= 0 or pd.isna(entry_price):
            continue

        # ATR-based stop sizing.
        atr_value = _compute_atr(df.iloc[: i + 1], period=atr_period)
        if atr_value is None or atr_value <= 0:
            continue
        stop_distance = atr_stop_multiple * atr_value
        stop_price = entry_price - stop_distance
        if stop_price >= entry_price:
            continue
        target_price = (
            entry_price + target_r_multiple * stop_distance
            if target_r_multiple is not None
            else None
        )

        signal_date = pd.Timestamp(rebal_date).date()
        fill_date = pd.Timestamp(df_index[i + 1]).date()
        signals.append(
            TradeSignal(
                ticker=ticker,
                setup_type=KIND,
                setup_grade="B",  # quant strategies use single grade for v1
                entry_date=signal_date,
                fill_date=fill_date,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                max_hold_days=max_hold_days,
                atr_at_signal=atr_value,
                notes={
                    "rebalance_date": str(rebal_date.date()),
                    "regime_ok": True,
                },
            )
        )
    return signals


def _compute_atr(df: pd.DataFrame, period: int) -> float | None:
    """Plain ATR(period) on the dataframe's last bar. Returns None if not enough data."""
    if len(df) < period + 1:
        return None
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    close = df["Close"].to_numpy(dtype=float)
    prev_close = np.roll(close, 1)
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    tr[0] = high[0] - low[0]  # first bar has no prev_close
    # Wilder's smoothing as a simple SMA fallback (close enough for sizing).
    return float(pd.Series(tr).rolling(window=period, min_periods=period).mean().iloc[-1])
