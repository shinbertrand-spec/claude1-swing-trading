"""Time-Series Momentum (TSMOM) per Moskowitz-Ooi-Pedersen 2012.

Per-ticker absolute-return-sign signal with an EXPLICIT cross-sectional top-K
rank — distinct from cross-sectional momentum (Clenow) which ranks by regression
score, and from dual-MA crossover which fires on MA crosses.

Logic:

* Every ``rebalance_period_days`` bars (on the benchmark calendar), compute each
  name's trailing ``lookback_days`` return.
* Keep only names with POSITIVE trailing return (the TSMOM sign rule — this is
  what makes it regime-defensive: in a down month few/no names qualify).
* Rank the survivors by trailing return DESC and keep the top ``top_k`` (default
  8, = the framework's concurrent-position cap).
* Emit a long signal at the next bar's open for each top-K name; ATR-based fixed
  stop; max-hold = rebalance period.

## Why the top-K rank exists (the 2026-06-05 fix)

The original v1 had NO ranking: every positive-trailing-return name emitted a
signal, and on a 1178-ticker universe a bull-market month fired hundreds of
them. The 8-concurrent cap then rejected ~98.5% of signals (45,375 / 46,083 on
``ts_momentum_liquid_us``), and which ~1.5% actually reached the equity curve was
decided by the simulator's arbitrary tie-break, NOT by the TSMOM signal. The
gate's Sharpe was therefore measuring the tie-break heuristic, not time-series
momentum — a Knight-Capital-genus failure (architecture silently rejecting work
the operator believed was happening). Ranking by signal strength and capping at
top-K makes the deployed strategy reproducibly the TSMOM signal.
"""
from __future__ import annotations

from typing import NamedTuple

import numpy as np
import pandas as pd

from ...backtest.setup_replay import TradeSignal


KIND = "ts_momentum"

DEFAULT_TOP_K = 8  # match the framework's max-concurrent-positions cap


class CrossSectionalState(NamedTuple):
    """Pre-computed top-K rank state.

    ``ranks_by_date`` maps each rebalance date to the set of top-K tickers on
    that date. ``score_by_date`` maps date -> {ticker: trailing_return} for the
    top-K (carried into signal notes). ``rebalance_dates`` is the benchmark-
    calendar rebalance schedule.
    """
    ranks_by_date: dict[pd.Timestamp, set[str]]
    score_by_date: dict[pd.Timestamp, dict[str, float]]
    rebalance_dates: list[pd.Timestamp]


def _rebalance_calendar(universe_dfs: dict[str, pd.DataFrame], benchmark) -> list:
    """Common rebalance calendar: the benchmark's index if available, else the
    longest ticker index (so ranking dates are shared across the universe)."""
    if benchmark and benchmark in universe_dfs:
        return list(universe_dfs[benchmark].index)
    best: list = []
    for df in universe_dfs.values():
        if len(df.index) > len(best):
            best = list(df.index)
    return best


def _trailing_return_at(df: pd.DataFrame, d: pd.Timestamp, lookback: int) -> float | None:
    """Trailing ``lookback``-bar return ending at date ``d`` (no lookahead).
    Returns None if ``d`` is absent or there isn't ``lookback`` bars before it."""
    if d not in df.index:
        return None
    pos = df.index.get_loc(d)
    if not isinstance(pos, int) or pos < lookback:
        return None
    closes = df["Close"]
    c_now = float(closes.iloc[pos])
    c_then = float(closes.iloc[pos - lookback])
    if c_then <= 0 or pd.isna(c_then) or pd.isna(c_now):
        return None
    return (c_now / c_then) - 1.0


def precompute(
    universe_dfs: dict[str, pd.DataFrame],
    params: dict,
) -> CrossSectionalState:
    """Build the per-rebalance-date top-K rank over the universe.

    Ranks positive-trailing-return names by trailing return DESC and keeps the
    top ``top_k`` per rebalance date. Point-in-time: each date uses only closes
    up to that date, so there is no lookahead.
    """
    benchmark = params.get("benchmark")
    lookback = int(params["lookback_days"])
    rebalance_period = int(params.get("rebalance_period_days", 21))
    top_k = int(params.get("top_k", DEFAULT_TOP_K))

    cal = _rebalance_calendar(universe_dfs, benchmark)
    if len(cal) < lookback + 2:
        return CrossSectionalState({}, {}, [])
    rebalance_dates = [cal[i] for i in range(lookback, len(cal), rebalance_period)]

    candidates = [t for t in universe_dfs if t != benchmark]
    ranks_by_date: dict[pd.Timestamp, set[str]] = {}
    score_by_date: dict[pd.Timestamp, dict[str, float]] = {}
    for d in rebalance_dates:
        scored: list[tuple[float, str]] = []
        for t in candidates:
            tr = _trailing_return_at(universe_dfs[t], d, lookback)
            if tr is None or tr <= 0:        # TSMOM sign gate
                continue
            scored.append((tr, t))
        scored.sort(reverse=True)
        top = scored[:top_k]
        ranks_by_date[d] = {t for _, t in top}
        score_by_date[d] = {t: r for r, t in top}
    return CrossSectionalState(ranks_by_date, score_by_date, rebalance_dates)


def replay(
    df: pd.DataFrame,
    ticker: str,
    params: dict,
    state: CrossSectionalState | None,
) -> list[TradeSignal]:
    """Emit a signal at each rebalance date where ``ticker`` is in the top-K."""
    if ticker == params.get("benchmark"):
        return []
    if state is None or not state.rebalance_dates:
        return []

    max_hold_days = int(params.get("max_hold_days", params.get("rebalance_period_days", 21)))
    atr_period = int(params.get("atr_period", 20))
    atr_stop_multiple = float(params.get("atr_stop_multiple", 3.0))

    if "Open" not in df.columns or "Close" not in df.columns:
        raise ValueError(f"{ticker}: df missing Open/Close columns")

    signals: list[TradeSignal] = []
    df_index = list(df.index)
    for rebal_date in state.rebalance_dates:
        if ticker not in state.ranks_by_date.get(rebal_date, set()):
            continue
        if rebal_date not in df.index:
            continue
        i = df_index.index(rebal_date)
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
                target_price=None,
                max_hold_days=max_hold_days,
                atr_at_signal=atr_value,
                notes={
                    "trailing_return": state.score_by_date.get(rebal_date, {}).get(ticker),
                    "rebalance_date": str(signal_date),
                    "rank_of_k": params.get("top_k", DEFAULT_TOP_K),
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
