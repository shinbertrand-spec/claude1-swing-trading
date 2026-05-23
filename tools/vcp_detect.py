"""Volatility Contraction Pattern detector (per ``swing-setup-library.md``).

VCP criteria (Minervini):

* 2-6 progressive pullbacks visible on the daily chart over the past 4-12 weeks
* Each pullback strictly smaller than the previous (depth decreasing)
* Final contraction ≤ 5% volatility
* Current price breaks above the pivot point (resistance = high of the most
  recent contraction)
* Breakout volume ≥ 40-50% above 20-day average

This Phase 2 baseline uses peak/trough detection with a centred-window swing
filter. Heuristic — final tuning belongs to walk-forward calibration (Phase 5).
Tagged accordingly below.

CLI::

    uv run python -m tools.vcp_detect AAPL --weeks 12
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from .cli import emit
from .contract import TraceEntry
from .data import fetch_ohlcv

TOOL = "tools/vcp_detect.py"

# Phase 2 baseline thresholds. Refine after walk-forward calibration (Phase 5).
MAX_FINAL_CONTRACTION_PCT = 5.0
MIN_PULLBACKS = 2
MAX_PULLBACKS = 6
SWING_WINDOW = 5  # bars on each side to qualify as a local extreme
BREAKOUT_VOLUME_RATIO_THRESHOLD = 1.40


def _find_swings(close: pd.Series, window: int = SWING_WINDOW) -> tuple[list[int], list[int]]:
    """Return (peak_idx, trough_idx) using centred-window comparison.

    A bar at position i is a peak iff Close[i] > Close[i-w..i-1] and
    Close[i] > Close[i+1..i+w]. Mirrored for troughs. Endpoints excluded.
    """
    peaks: list[int] = []
    troughs: list[int] = []
    values = close.to_numpy()
    for i in range(window, len(values) - window):
        left = values[i - window : i]
        right = values[i + 1 : i + 1 + window]
        if values[i] > left.max() and values[i] > right.max():
            peaks.append(i)
        elif values[i] < left.min() and values[i] < right.min():
            troughs.append(i)
    return peaks, troughs


def _pair_contractions(
    peaks: list[int], troughs: list[int], values: np.ndarray
) -> list[dict]:
    """Walk left to right pairing each peak with the next trough.

    Returns a list of contractions, each ``{peak_idx, trough_idx, peak,
    trough, depth_pct}`` where ``depth_pct = (peak - trough) / peak * 100``.
    """
    contractions: list[dict] = []
    p_iter = iter(peaks)
    t_iter = iter(troughs)
    peak_i = next(p_iter, None)
    trough_i = next(t_iter, None)
    while peak_i is not None and trough_i is not None:
        # advance trough to be after current peak
        while trough_i is not None and trough_i <= peak_i:
            trough_i = next(t_iter, None)
        if trough_i is None:
            break
        peak_val = float(values[peak_i])
        trough_val = float(values[trough_i])
        depth_pct = (peak_val - trough_val) / peak_val * 100.0 if peak_val > 0 else 0.0
        contractions.append(
            {
                "peak_idx": peak_i,
                "trough_idx": trough_i,
                "peak": peak_val,
                "trough": trough_val,
                "depth_pct": depth_pct,
            }
        )
        # advance peak past current trough
        while peak_i is not None and peak_i <= trough_i:
            peak_i = next(p_iter, None)
    return contractions


def compute_from_ohlcv(
    df: pd.DataFrame,
    weeks: int = 12,
    swing_window: int = SWING_WINDOW,
    max_final_pct: float = MAX_FINAL_CONTRACTION_PCT,
) -> TraceEntry:
    """Detect a VCP in the trailing ``weeks`` of daily bars.

    Args:
        df: OHLCV DataFrame. Needs ``Close`` and ``Volume``.
        weeks: lookback window in weeks (1 week = 5 trading days).
        swing_window: half-width for swing-point detection.
        max_final_pct: final-contraction depth ceiling. Defaults to 5 (%).
    """
    required = {"Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing required columns: {sorted(missing)}")

    bars = weeks * 5
    if len(df) < bars + swing_window:
        raise ValueError(f"need at least {bars + swing_window} bars; got {len(df)}")

    window = df.tail(bars).reset_index(drop=False)
    close = window["Close"].astype(float)
    volume = window["Volume"].astype(float)

    peaks, troughs = _find_swings(close, window=swing_window)
    contractions = _pair_contractions(peaks, troughs, close.to_numpy())

    n = len(contractions)
    progressive = False
    final_depth_pct: float | None = None
    pivot: float | None = None
    pivot_idx: int | None = None
    if n >= MIN_PULLBACKS:
        depths = [c["depth_pct"] for c in contractions]
        progressive = all(depths[i] < depths[i - 1] for i in range(1, n))
        final_depth_pct = depths[-1]
        # Pivot = the most recent peak (resistance of the final contraction).
        pivot_idx = contractions[-1]["peak_idx"]
        pivot = float(close.iloc[pivot_idx])

    # Volume confirmation: today vs 20-day average.
    vol_today = float(volume.iloc[-1])
    vol_20d_avg = float(volume.tail(21).iloc[:-1].mean())  # prior 20 bars
    vol_ratio = vol_today / vol_20d_avg if vol_20d_avg > 0 else 0.0

    last_close = float(close.iloc[-1])
    breakout_confirmed = (
        pivot is not None
        and last_close > pivot
        and vol_ratio >= BREAKOUT_VOLUME_RATIO_THRESHOLD
    )
    detected = (
        n >= MIN_PULLBACKS
        and n <= MAX_PULLBACKS
        and progressive
        and final_depth_pct is not None
        and final_depth_pct <= max_final_pct
    )

    return TraceEntry(
        tool=TOOL,
        inputs={
            "weeks": weeks,
            "swing_window": swing_window,
            "max_final_pct": max_final_pct,
            "rows": len(df),
            "last_close_date": str(df.index[-1]),
        },
        output={
            "detected": detected,
            "contractions_count": n,
            "contractions": [
                {"depth_pct": c["depth_pct"], "peak": c["peak"], "trough": c["trough"]}
                for c in contractions
            ],
            "progressive": progressive,
            "final_depth_pct": final_depth_pct,
            "pivot": pivot,
            "last_close": last_close,
            "above_pivot": (last_close > pivot) if pivot is not None else False,
            "volume_today": vol_today,
            "volume_20d_avg": vol_20d_avg,
            "volume_ratio": vol_ratio,
            "breakout_confirmed": breakout_confirmed,
            "phase2_baseline_note": "Heuristic implementation; refine after walk-forward calibration.",
        },
    )


def compute_from_ticker(ticker: str, weeks: int = 12) -> TraceEntry:
    fetch = fetch_ohlcv(ticker, period="6mo")
    entry = compute_from_ohlcv(fetch.df, weeks=weeks)
    entry.inputs = {
        **entry.inputs,
        "ticker": ticker,
        "source": fetch.source,
        "data_fetched_at": fetch.fetched_at,
    }
    return entry


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.vcp_detect",
        description="Detect VCP (progressive contractions + breakout) on daily bars.",
    )
    p.add_argument("ticker")
    p.add_argument("--weeks", type=int, default=12)
    args = p.parse_args()
    emit(compute_from_ticker(args.ticker, weeks=args.weeks))


if __name__ == "__main__":
    main()
