"""Cross-sectional short-term reversal strategy.

Per Lehmann 1990 / Jegadeesh 1990 / paperswithbacktest catalog
"Short Term Reversal Effect in Stocks" (in-sample Sharpe ~0.816 weekly).

Long-only adaptation for Claude1's swing framework:

* Rebalance weekly (5 trading days).
* Rank universe by trailing ``lookback_days``-day return.
* Long the bottom ``bottom_n`` tickers (biggest 1-week losers — bet on
  mean-reversion bounce).
* Hold ``max_hold_days`` bars (typically same as rebalance period).
* ATR-based stop (typically tight — MR strategies want quick exits).

Long-only is structurally weaker than the paper's long-short form; the
expected OOS Sharpe will be materially below the published 0.816. The
test is whether the *long leg alone* clears Claude1's deployment gate
on the SP500-leaning 88-ticker universe.
"""
from __future__ import annotations

from typing import NamedTuple

import numpy as np
import pandas as pd

from ...backtest.setup_replay import TradeSignal


KIND = "xs_short_term_reversal"


class CrossSectionalState(NamedTuple):
    """Cross-sectional bottom-N ranking state per rebalance date.

    ``bottom_n_by_date`` maps each rebalance date to the set of tickers
    that ranked in the bottom-N (lowest trailing returns) on that date.
    """
    bottom_n_by_date: dict[pd.Timestamp, set[str]]
    rebalance_dates: list[pd.Timestamp]


def _trailing_return(closes: pd.Series, lookback: int) -> float:
    """Simple trailing return over ``lookback`` bars. NaN if insufficient."""
    if len(closes) < lookback + 1:
        return float("nan")
    window = closes.iloc[-(lookback + 1):].to_numpy()
    if np.any(window <= 0) or np.any(np.isnan(window)):
        return float("nan")
    return float(window[-1] / window[0] - 1.0)


def precompute(
    universe_dfs: dict[str, pd.DataFrame],
    params: dict,
) -> CrossSectionalState:
    """Build bottom-N ranking state for each rebalance date.

    Selection size accepts either form (``bottom_pct`` wins when both are
    set so a wider-universe migration only needs the spec to add
    ``bottom_pct`` without removing ``bottom_n``):

    * ``bottom_pct: 0.05`` — pick the bottom 5% of the candidate pool
      (count derived as ``max(1, int(n_candidates * bottom_pct))``).
      Use this on universes that change size (e.g. S&P 500 vs the 88
      mega-cap pool) — keeps the strategy's selectivity ratio stable.
    * ``bottom_n: 5`` — absolute count. Use only on fixed universes.
    """
    benchmark = params["benchmark"]
    lookback = int(params["lookback_days"])
    rebalance_period = int(params["rebalance_period_days"])

    if benchmark not in universe_dfs:
        raise ValueError(
            f"benchmark {benchmark!r} not in universe_dfs; add it to the spec's universe.tickers"
        )

    # Use benchmark's dates as the calendar (any common-traded date).
    bench_dates = list(universe_dfs[benchmark].index)
    if len(bench_dates) < lookback + 2:
        return CrossSectionalState({}, [])
    start_idx = lookback + 1
    rebalance_dates = [bench_dates[i] for i in range(start_idx, len(bench_dates), rebalance_period)]

    candidate_tickers = [t for t in universe_dfs if t != benchmark]

    # Resolve selection size from params.
    if "bottom_pct" in params and params["bottom_pct"] is not None:
        bottom_pct = float(params["bottom_pct"])
        if not 0.0 < bottom_pct <= 1.0:
            raise ValueError(f"bottom_pct must be in (0, 1]; got {bottom_pct}")
        bottom_n = max(1, int(len(candidate_tickers) * bottom_pct))
    elif "bottom_n" in params and params["bottom_n"] is not None:
        bottom_n = int(params["bottom_n"])
    else:
        raise ValueError("xs_short_term_reversal needs bottom_pct or bottom_n in params")
    bottom_n_by_date: dict[pd.Timestamp, set[str]] = {}
    for d in rebalance_dates:
        scores: list[tuple[float, str]] = []
        for t in candidate_tickers:
            tdf = universe_dfs[t]
            if d not in tdf.index:
                continue
            closes_through = tdf["Close"].loc[:d]
            r = _trailing_return(closes_through, lookback)
            if np.isfinite(r):
                scores.append((r, t))
        # Sort ASCENDING — bottom-N = biggest losers.
        scores.sort()
        bottom_n_by_date[d] = {t for _, t in scores[:bottom_n]}

    return CrossSectionalState(bottom_n_by_date, rebalance_dates)


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
    atr_stop_multiple = float(params.get("atr_stop_multiple", 2.0))
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
        if ticker not in state.bottom_n_by_date.get(rebal_date, set()):
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
                notes={"rebalance_date": str(rebal_date.date())},
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
