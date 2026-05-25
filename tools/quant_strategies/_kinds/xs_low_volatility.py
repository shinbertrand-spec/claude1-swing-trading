"""Cross-sectional low-volatility factor strategy.

Per Blitz & van Vliet 2007 / paperswithbacktest catalog "Low Volatility
Factor Effect in Stocks" (in-sample Sharpe ~0.717 monthly).

Long-only adaptation:

* Rebalance monthly (21 trading days).
* Rank universe by trailing ``lookback_days``-day **realized
  volatility** (annualised std of daily log returns).
* Long the bottom-N (lowest-vol names).
* Hold ``max_hold_days`` bars.
* ATR-based stop.

## v2 — SPY 200d MA regime filter

The v1 release (commit ec4d5af area) failed the rolling-walk-forward
deployment gate across all three grid combos (bottom_n in {10, 15, 20})
because the naked-long port inherits market beta during regime breaks
— see ``[[swing-xs-low-volatility-port-rejected]]``. The 2023 OOS
window (rates shock) blew DD past −30%.

v2 adds the same regime filter Clenow uses: only enter when the
benchmark (SPY) closes above its ``regime_filter_period``-day SMA
(default 200). When the regime is off, the strategy sits in cash for
that rebalance. The vault note's estimate: ~half the 2023 DD if the
strategy sits out the rate-shock months.

The filter is precomputed per rebalance date in ``precompute()`` and
applied at signal emission in ``replay()`` — same shape as
``clenow_momentum.py`` so the two strategies stay symmetric.

Hypothesis: the low-vol anomaly is the most-replicated equity-factor
finding outside of momentum. On Claude1's mega-cap-tilted 88-ticker
universe (heavy on FAANG-class large-caps), low-vol effects may be
muted vs the broader equity baskets the original papers used. The
deployment-gate verdict is the empirical test.
"""
from __future__ import annotations

from typing import NamedTuple

import numpy as np
import pandas as pd

from ...backtest.setup_replay import TradeSignal


KIND = "xs_low_volatility"


class CrossSectionalState(NamedTuple):
    """Cross-sectional bottom-N (lowest-vol) state per rebalance date.

    ``benchmark_regime_ok`` maps each date in the benchmark's index to
    whether the benchmark closed above its SMA on that date. Empty when
    the regime filter is disabled (``regime_filter_period <= 0``).
    """
    bottom_n_by_date: dict[pd.Timestamp, set[str]]
    rebalance_dates: list[pd.Timestamp]
    benchmark_regime_ok: dict[pd.Timestamp, bool]


def _realized_volatility(closes: pd.Series, lookback: int) -> float:
    """Annualised realized vol = std(log_returns) * sqrt(252).

    Returns NaN if insufficient data or all-equal closes.
    """
    if len(closes) < lookback + 1:
        return float("nan")
    window = closes.iloc[-(lookback + 1):].to_numpy()
    if np.any(window <= 0) or np.any(np.isnan(window)):
        return float("nan")
    log_returns = np.diff(np.log(window))
    if len(log_returns) < 2:
        return float("nan")
    std = float(np.std(log_returns, ddof=1))
    if std == 0.0:
        return float("nan")
    return std * np.sqrt(252.0)


def precompute(
    universe_dfs: dict[str, pd.DataFrame],
    params: dict,
) -> CrossSectionalState:
    """Build bottom-N (lowest-vol) ranking state per rebalance date,
    plus the SPY > SMA regime-pass dict (v2)."""
    benchmark = params["benchmark"]
    lookback = int(params["lookback_days"])
    bottom_n = int(params["bottom_n"])
    rebalance_period = int(params["rebalance_period_days"])
    regime_period = int(params.get("regime_filter_period", 200))

    if benchmark not in universe_dfs:
        raise ValueError(
            f"benchmark {benchmark!r} not in universe_dfs; add it to the spec's universe.tickers"
        )

    bench_df = universe_dfs[benchmark]
    bench_close = bench_df["Close"]

    # Regime filter precompute. When disabled (regime_period <= 0), leave
    # the dict empty and replay treats absent keys as regime-ok.
    benchmark_regime_ok: dict[pd.Timestamp, bool] = {}
    if regime_period > 0:
        bench_sma = bench_close.rolling(
            window=regime_period, min_periods=regime_period,
        ).mean()
        benchmark_regime_ok = {
            d: bool(bench_close.loc[d] > bench_sma.loc[d])
            for d in bench_close.index
            if not pd.isna(bench_sma.loc[d])
        }

    bench_dates = list(bench_close.index)
    min_history = max(lookback + 1, regime_period if regime_period > 0 else 0)
    if len(bench_dates) < min_history + 1:
        return CrossSectionalState({}, [], benchmark_regime_ok)
    start_idx = min_history
    rebalance_dates = [bench_dates[i] for i in range(start_idx, len(bench_dates), rebalance_period)]

    candidate_tickers = [t for t in universe_dfs if t != benchmark]
    bottom_n_by_date: dict[pd.Timestamp, set[str]] = {}
    for d in rebalance_dates:
        scores: list[tuple[float, str]] = []
        for t in candidate_tickers:
            tdf = universe_dfs[t]
            if d not in tdf.index:
                continue
            closes_through = tdf["Close"].loc[:d]
            vol = _realized_volatility(closes_through, lookback)
            if np.isfinite(vol):
                scores.append((vol, t))
        # Sort ASCENDING — bottom-N = lowest vol.
        scores.sort()
        bottom_n_by_date[d] = {t for _, t in scores[:bottom_n]}

    return CrossSectionalState(bottom_n_by_date, rebalance_dates, benchmark_regime_ok)


def replay(
    df: pd.DataFrame,
    ticker: str,
    params: dict,
    state: CrossSectionalState,
) -> list[TradeSignal]:
    """Emit a signal each rebalance date the ticker is in the bottom-N."""
    if not state.rebalance_dates:
        return []

    atr_period = int(params.get("atr_period", 20))
    atr_stop_multiple = float(params.get("atr_stop_multiple", 2.5))
    rebalance_period = int(params["rebalance_period_days"])
    max_hold_days = int(params.get("max_hold_days", rebalance_period))
    target_r_multiple = params.get("target_r_multiple")
    if target_r_multiple is not None:
        target_r_multiple = float(target_r_multiple)

    if "Open" not in df.columns or "Close" not in df.columns:
        raise ValueError(f"{ticker}: df missing Open/Close columns")

    # When the regime-pass dict is empty (filter disabled in v1-compat
    # config), every date is considered regime-ok. When non-empty, only
    # dates with regime_ok=True can produce signals.
    regime_filter_active = bool(state.benchmark_regime_ok)

    signals: list[TradeSignal] = []
    df_index = list(df.index)
    for rebal_date in state.rebalance_dates:
        if ticker not in state.bottom_n_by_date.get(rebal_date, set()):
            continue
        if regime_filter_active and not state.benchmark_regime_ok.get(rebal_date, False):
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
                    "regime_ok": True if regime_filter_active else None,
                },
            )
        )
    return signals


def _compute_atr(df: pd.DataFrame, period: int) -> float | None:
    """Plain ATR(period) on the last bar. Returns None if not enough data."""
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
