"""Connors Cumulative RSI(2) mean-reversion with 200d-SMA regime filter.

Per Larry Connors, *Short Term Trading Strategies That Work* (2008) and
the quantitativo.com 2025 re-validation ("Squeezing more profits with
cumulative RSI"):

Entry (any trading day on which BOTH hold):
* SPY close > SPY 200d SMA (regime filter — bull regime only)
* Per-ticker 2-period cumulative RSI < ``entry_threshold`` (default 10)

The 2-period cumulative RSI is the sum of today's 2-period RSI and
yesterday's 2-period RSI. Lower values = deeper short-term oversold.

Exit (whichever first):
* Max-hold ``max_hold_days`` bars reached (default 5 — typical Connors hold)
* Stop hit (ATR × ``atr_stop_multiple`` below entry)

Connors' canonical exit is "CRSI > 65" but that is per-bar dynamic and
requires a custom sell-decision detector. v1 of this plugin uses a tight
max-hold instead — the bulk of the mean-reversion edge fires within 3-5
bars, and max-hold + ATR stop captures it.

The 200d-SMA regime filter is the KEY cross-regime feature. In a
prolonged bear regime (e.g. most of 2022 below SPY's 200d), the strategy
generates NO new entries — so the worst case for any window is "flat",
not "deep loss". This is the structural property that should let it
clear the 5/6-windows-above-Sharpe-0.5 bar.
"""
from __future__ import annotations

from typing import NamedTuple

import numpy as np
import pandas as pd

from ...backtest.setup_replay import TradeSignal


KIND = "connors_rsi2"


class ConnorsRsi2State(NamedTuple):
    """Per-date entry-eligibility map.

    ``eligible_by_date[d]`` = set of tickers whose 2-period cumulative
    RSI on day ``d`` is below ``entry_threshold`` AND SPY closed above
    its 200d SMA on ``d``.
    """
    eligible_by_date: dict[pd.Timestamp, set[str]]
    benchmark_dates: list[pd.Timestamp]


def _rsi(closes: pd.Series, period: int) -> pd.Series:
    """Standard Wilder RSI on a close-price series. NaN until ``period`` bars in.

    For period=2 (Connors' canonical), the EMA smoothing is short enough
    that we compute via simple ratio of last ``period`` up-moves to
    down-moves rather than Wilder's iterative smoothing. This matches the
    common practitioner implementations seen in Connors/quantitativo.
    """
    diff = closes.diff()
    up = diff.clip(lower=0.0)
    down = (-diff).clip(lower=0.0)
    # EMA-style smoothing with alpha = 1/period (Wilder).
    avg_up = up.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_down = down.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_up / avg_down.replace(0.0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    # When avg_down is 0 (all up bars in lookback), RSI = 100.
    rsi = rsi.where(avg_down > 0, 100.0)
    return rsi


def _cumulative_rsi(closes: pd.Series, rsi_period: int, cum_period: int) -> pd.Series:
    """Sum of the last ``cum_period`` values of RSI(``rsi_period``)."""
    rsi = _rsi(closes, rsi_period)
    return rsi.rolling(window=cum_period, min_periods=cum_period).sum()


def precompute(
    universe_dfs: dict[str, pd.DataFrame],
    params: dict,
) -> ConnorsRsi2State:
    """Build per-day eligibility map respecting the regime filter."""
    benchmark = params["benchmark"]
    if benchmark not in universe_dfs:
        raise ValueError(
            f"benchmark {benchmark!r} not in universe_dfs; add it to spec's universe.tickers"
        )

    rsi_period = int(params.get("rsi_period", 2))
    cum_period = int(params.get("cumulative_period", 2))
    entry_threshold = float(params.get("entry_threshold", 10.0))
    regime_sma_period = int(params.get("regime_sma_period", 200))

    bench_df = universe_dfs[benchmark]
    bench_close = bench_df["Close"].astype(float)
    bench_sma = bench_close.rolling(window=regime_sma_period, min_periods=regime_sma_period).mean()
    bench_above_sma = bench_close > bench_sma  # bool Series, NaN-SMA → False

    bench_dates = list(bench_df.index)
    if len(bench_dates) < max(regime_sma_period, rsi_period + cum_period) + 1:
        return ConnorsRsi2State({}, [])

    # Pre-compute per-ticker cumulative RSI series once.
    ticker_crsi: dict[str, pd.Series] = {}
    candidate_tickers = [t for t in universe_dfs if t != benchmark]
    for t in candidate_tickers:
        tdf = universe_dfs[t]
        closes = tdf["Close"].astype(float)
        ticker_crsi[t] = _cumulative_rsi(closes, rsi_period, cum_period)

    # Per-day concurrency cap. Connors' canonical max-3 enforces this at
    # the broker level, but Claude1's per-ticker independent simulator has
    # no portfolio-level cap; without this knob, a wider universe fires
    # 10-100x more concurrent entries on the same regime-on day. Setting
    # ``max_concurrent_positions: N`` ranks eligible names by deepest
    # oversold (lowest cumulative-RSI) and keeps the N most-oversold.
    max_concurrent = params.get("max_concurrent_positions")
    if max_concurrent is not None:
        max_concurrent = int(max_concurrent)
        if max_concurrent <= 0:
            raise ValueError(f"max_concurrent_positions must be positive; got {max_concurrent}")

    eligible_by_date: dict[pd.Timestamp, set[str]] = {}
    for d in bench_dates:
        if not bool(bench_above_sma.get(d, False)):
            continue
        # Score = (cumulative_rsi, ticker) — ascending CRSI = deepest oversold.
        scored: list[tuple[float, str]] = []
        for t in candidate_tickers:
            crsi_series = ticker_crsi[t]
            if d not in crsi_series.index:
                continue
            v = crsi_series.loc[d]
            if pd.notna(v) and v < entry_threshold:
                scored.append((float(v), t))
        if not scored:
            continue
        if max_concurrent is not None and len(scored) > max_concurrent:
            scored.sort()  # ascending CRSI = most oversold first
            scored = scored[:max_concurrent]
        eligible_by_date[d] = {t for _, t in scored}

    return ConnorsRsi2State(eligible_by_date, bench_dates)


def replay(
    df: pd.DataFrame,
    ticker: str,
    params: dict,
    state: ConnorsRsi2State,
) -> list[TradeSignal]:
    """Emit one TradeSignal per (ticker, date) the ticker is eligible.

    Entry is at next-bar Open (after the signal day's close confirmed
    eligibility). Stop is ATR-based; no fixed target. Max-hold captures
    the typical 3-5 bar mean-reversion window.
    """
    if not state.eligible_by_date:
        return []
    if "Open" not in df.columns or "Close" not in df.columns:
        raise ValueError(f"{ticker}: df missing Open/Close columns")

    atr_period = int(params.get("atr_period", 20))
    atr_stop_multiple = float(params.get("atr_stop_multiple", 2.0))
    max_hold_days = int(params.get("max_hold_days", 5))
    target_r_multiple = params.get("target_r_multiple")
    if target_r_multiple is not None:
        target_r_multiple = float(target_r_multiple)

    # Cooldown: avoid re-entering the same name within ``cooldown_days``
    # bars of an entry — without this, a deep-oversold name fires daily
    # for a week. Connors caps concurrent positions at 3; for a long-
    # only backtest the cooldown achieves a similar effect per-ticker.
    cooldown_days = int(params.get("cooldown_days", 5))

    df_index = list(df.index)
    signals: list[TradeSignal] = []
    last_entry_idx = -10**9
    for signal_date in state.eligible_by_date:
        if ticker not in state.eligible_by_date[signal_date]:
            continue
        if signal_date not in df.index:
            continue
        try:
            i = df_index.index(signal_date)
        except ValueError:
            continue
        if i - last_entry_idx < cooldown_days:
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

        sig_d = pd.Timestamp(signal_date).date()
        fill_d = pd.Timestamp(df_index[i + 1]).date()
        signals.append(
            TradeSignal(
                ticker=ticker,
                setup_type=KIND,
                setup_grade="B",
                entry_date=sig_d,
                fill_date=fill_d,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                max_hold_days=max_hold_days,
                atr_at_signal=atr_value,
                notes={"crsi_signal_date": str(sig_d)},
            )
        )
        last_entry_idx = i

    return signals


def _compute_atr(df: pd.DataFrame, period: int) -> float | None:
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
