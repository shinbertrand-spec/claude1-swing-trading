"""Residual momentum strategy (Blitz-Huij-Martens 2011 / Kakushadze 2018 §3.7).

Architecturally identical to :mod:`clenow_momentum`: cross-sectional top-K
selection, weekly rebalance, ATR-based stops, SPY-200d regime filter. The
ONLY difference is the per-ticker score function — instead of fitting
slope × R² to raw log prices, we first strip the market-beta exposure
from each ticker's return series and then fit slope × R² to the
cumulative residual log-return path.

The thesis: in a heterogeneous-beta universe (small/micro caps with
betas 0.4-2.0+) raw cross-sectional momentum over-concentrates into
high-beta names during bull regimes. Residualisation removes the
beta-amplified component, so the top-K is selected on idiosyncratic
strength rather than beta-amplified market drift. Per the vault
concept page [[residual-momentum]], the canonical empirical anchor is:

  Blitz, Huij & Martens (2011) — "Residual Momentum" — Journal of
  Empirical Finance. IR ~0.8 on residual long-short vs IR ~0.5 on
  raw, multi-decade sample.

## Lookahead discipline (critical)

The per-ticker beta MUST be estimated using only data up to and
including the rebalance date `d`. The function signature enforces
this: :func:`_residual_momentum_score` only sees ``ticker_closes`` /
``benchmark_closes`` slices that the caller has already truncated to
``[:d]``. The caller (`precompute`) follows the same `.loc[:d]` slice
pattern as :mod:`clenow_momentum`.

Tests in ``tests/test_quant_strategies_residual_momentum.py`` include
an explicit no-lookahead property: scrambling post-`d` data must NOT
change the score computed at `d`.

## v1 caveats

* Single-factor (market only, SPY). Multi-factor extensions (market +
  SMB + HML) are a future enhancement — Blitz 2011's primary table is
  market-only.
* Same TradeSignal-compatibility shim as Clenow v1: one signal per
  rebalance date when ticker is in top-K. Phase 5.d portfolio
  simulator would enable faithful continuous holds.
"""
from __future__ import annotations

from typing import NamedTuple

import numpy as np
import pandas as pd

from ...backtest.setup_replay import TradeSignal


KIND = "residual_momentum"


class CrossSectionalState(NamedTuple):
    """Per-rebalance ranking + regime state. Same shape as clenow_momentum."""
    ranks_by_date: dict[pd.Timestamp, set[str]]
    benchmark_regime_ok: dict[pd.Timestamp, bool]
    rebalance_dates: list[pd.Timestamp]


def _residual_momentum_score(
    ticker_closes: pd.Series,
    benchmark_closes: pd.Series,
    lookback: int,
) -> float:
    """Score a ticker by the slope × R² of its cumulative residual log-return path.

    Pipeline (point-in-time — no lookahead):

    1. Log-returns of ticker + benchmark over the lookback window.
    2. OLS: ``ticker_ret = alpha + beta * benchmark_ret + eps``.
    3. Residual returns: ``eps_t = ticker_ret_t - (alpha + beta * benchmark_ret_t)``.
    4. Cumulative residual log-return path: ``cumsum(eps_t)``.
    5. Linear regression of the cumulative residual path vs time:
       slope (annualised, log → return) × R² is the score.

    Returns float("-inf") on insufficient data, degenerate regression
    (zero-variance benchmark), or any NaN exposure. The caller (the
    cross-sectional ranker) treats -inf as "not eligible this period."
    """
    if len(ticker_closes) < lookback + 1 or len(benchmark_closes) < lookback + 1:
        return float("-inf")

    # Daily log returns over the full available history; we'll trim to
    # the lookback window after dropping NaN to ensure 1:1 alignment.
    t_log_ret = np.log(ticker_closes / ticker_closes.shift(1))
    m_log_ret = np.log(benchmark_closes / benchmark_closes.shift(1))

    # Align ticker + benchmark on the date index; drop the leading NaN
    # from the first .shift(1) and any other NaNs (one side missing a day).
    aligned = pd.concat([t_log_ret.rename("t"), m_log_ret.rename("m")], axis=1).dropna()
    if len(aligned) < lookback:
        return float("-inf")
    aligned = aligned.iloc[-lookback:]
    t_arr = aligned["t"].to_numpy()
    m_arr = aligned["m"].to_numpy()

    # Benchmark must have nonzero variance; otherwise beta is undefined.
    if np.var(m_arr) <= 0:
        return float("-inf")
    if np.any(np.isnan(t_arr)) or np.any(np.isnan(m_arr)):
        return float("-inf")

    # OLS: regress ticker returns on benchmark returns.
    beta, alpha = np.polyfit(m_arr, t_arr, 1)
    residuals = t_arr - (alpha + beta * m_arr)

    # Cumulative residual log-return path = the "residual price index."
    cum_resid = np.cumsum(residuals)
    x = np.arange(len(cum_resid), dtype=float)

    # Slope × R² on the cumulative residual path — same metric as Clenow,
    # applied to the residual series rather than raw log prices.
    slope, intercept = np.polyfit(x, cum_resid, 1)
    fitted = slope * x + intercept
    ss_res = np.sum((cum_resid - fitted) ** 2)
    ss_tot = np.sum((cum_resid - cum_resid.mean()) ** 2)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    # Annualise the daily log-slope to a return-equivalent (matches Clenow).
    annualised = np.exp(slope * 252) - 1.0
    return float(annualised * max(r2, 0.0))


def precompute(
    universe_dfs: dict[str, pd.DataFrame],
    params: dict,
) -> CrossSectionalState:
    """Build the cross-sectional rebalance state.

    Same control flow as :func:`clenow_momentum.precompute` — only the
    per-ticker score function changes. The benchmark series is needed
    both for the regime filter (SPY > 200d SMA) AND as the regression
    factor for residualisation, so we pull it once and reuse.
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

    bench_dates = list(bench_close.index)
    if len(bench_dates) < max(lookback, regime_period) + 1:
        return CrossSectionalState({}, benchmark_regime_ok, [])
    start_idx = max(lookback, regime_period)
    rebalance_dates = [bench_dates[i] for i in range(start_idx, len(bench_dates), rebalance_period)]

    candidate_tickers = [t for t in universe_dfs if t != benchmark]
    ranks_by_date: dict[pd.Timestamp, set[str]] = {}
    for d in rebalance_dates:
        bench_closes_through = bench_close.loc[:d]
        scores: list[tuple[float, str]] = []
        for t in candidate_tickers:
            tdf = universe_dfs[t]
            if d not in tdf.index:
                continue
            ticker_closes_through = tdf["Close"].loc[:d]
            score = _residual_momentum_score(
                ticker_closes_through, bench_closes_through, lookback,
            )
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
    """Per-ticker signal emission. Identical to :func:`clenow_momentum.replay`.

    The kind plugin contract is uniform across cross-sectional strategies;
    only the scoring function (and thus what gets into `state.ranks_by_date`)
    differs. We could refactor this into a shared base, but at v1 the
    duplication is intentional — keeps each kind plugin readable in
    isolation. Phase 5.d portfolio simulator work is the natural time
    to factor out the shared replay logic.
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
                setup_grade="B",
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
    """ATR(period). Identical to clenow_momentum._compute_atr."""
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
    tr[0] = high[0] - low[0]
    return float(pd.Series(tr).rolling(window=period, min_periods=period).mean().iloc[-1])
