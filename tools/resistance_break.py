"""Resistance-breakout detector (per ``swing-setup-library.md`` Secondary 3).

Non-VCP horizontal-resistance breakout. Criteria:

* Clean horizontal resistance violated decisively (today's close > resistance)
* Volume ≥ 1.5× 20-day average on breakout
* Stage 2 stock (caller verifies via :mod:`tools.trend_template`)

Per swing-setup-library: "Lower probability than VCP because the prior
structure isn't characterised; use only if SEPA setups are scarce."

Resistance detection (Phase 2 baseline): find swing highs in lookback
window; require at least ``min_touches`` highs within ``touch_band_pct``
of each other to qualify as a horizontal resistance line.

CLI::

    uv run python -m tools.resistance_break AVGO
"""
from __future__ import annotations

import argparse

import pandas as pd

from .cli import emit
from .contract import TraceEntry
from .data import fetch_ohlcv

TOOL = "tools/resistance_break.py"

LOOKBACK_BARS = 90
SWING_WINDOW = 5
MIN_TOUCHES = 2
TOUCH_BAND_PCT = 1.5              # within 1.5% to count as same resistance
BREAKOUT_VOLUME_RATIO = 1.50


def _find_swing_highs(close: pd.Series, window: int = SWING_WINDOW) -> list[int]:
    peaks: list[int] = []
    values = close.to_numpy()
    for i in range(window, len(values) - window):
        left = values[i - window : i]
        right = values[i + 1 : i + 1 + window]
        if values[i] > left.max() and values[i] > right.max():
            peaks.append(i)
    return peaks


def compute_from_ohlcv(df: pd.DataFrame) -> TraceEntry:
    """Detect a resistance breakout on the most recent bar.

    Args:
        df: OHLCV DataFrame. Needs Close, High, Volume.
    """
    required = {"Close", "High", "Volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")
    if len(df) < LOOKBACK_BARS + SWING_WINDOW + 1:
        raise ValueError(
            f"need at least {LOOKBACK_BARS + SWING_WINDOW + 1} bars; got {len(df)}"
        )

    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    volume = df["Volume"].astype(float)

    # Restrict to lookback window; exclude today.
    window = close.iloc[-(LOOKBACK_BARS + 1) : -1]
    peaks_local = _find_swing_highs(window)
    if len(peaks_local) < MIN_TOUCHES:
        return TraceEntry(
            tool=TOOL,
            inputs={
                "rows": len(df),
                "last_close_date": str(df.index[-1]),
            },
            output={
                "detected": False,
                "reason": f"need >= {MIN_TOUCHES} swing highs; got {len(peaks_local)}",
            },
        )

    peak_values = sorted([float(window.iloc[i]) for i in peaks_local], reverse=True)
    # Find the densest cluster of peaks within TOUCH_BAND_PCT of each other.
    best_cluster: list[float] = []
    for anchor in peak_values:
        band = anchor * (TOUCH_BAND_PCT / 100.0)
        cluster = [p for p in peak_values if abs(p - anchor) <= band]
        if len(cluster) > len(best_cluster):
            best_cluster = cluster
    if len(best_cluster) < MIN_TOUCHES:
        return TraceEntry(
            tool=TOOL,
            inputs={"rows": len(df), "last_close_date": str(df.index[-1])},
            output={
                "detected": False,
                "reason": f"no resistance cluster with {MIN_TOUCHES}+ touches within {TOUCH_BAND_PCT}%",
            },
        )

    resistance_level = sum(best_cluster) / len(best_cluster)

    today_close = float(close.iloc[-1])
    today_high = float(high.iloc[-1])
    today_volume = float(volume.iloc[-1])
    avg_vol_20 = float(volume.iloc[-21:-1].mean())
    vol_ratio = today_volume / avg_vol_20 if avg_vol_20 > 0 else 0.0

    broke_resistance = today_close > resistance_level
    decisive = today_close > resistance_level * 1.005  # at least 0.5% above
    volume_confirms = vol_ratio >= BREAKOUT_VOLUME_RATIO

    detected = broke_resistance and decisive and volume_confirms

    return TraceEntry(
        tool=TOOL,
        inputs={
            "rows": len(df),
            "last_close_date": str(df.index[-1]),
        },
        output={
            "detected": detected,
            "criteria": {
                "broke_resistance": broke_resistance,
                "decisive_break": decisive,
                "volume_confirms": volume_confirms,
            },
            "stats": {
                "resistance_level": resistance_level,
                "touches_in_cluster": len(best_cluster),
                "today_close": today_close,
                "today_high": today_high,
                "today_volume": today_volume,
                "volume_20d_avg": avg_vol_20,
                "volume_ratio": vol_ratio,
            },
            "suggested_pivot": resistance_level,
        },
    )


def compute_from_ticker(ticker: str) -> TraceEntry:
    fetch = fetch_ohlcv(ticker, period="6mo")
    entry = compute_from_ohlcv(fetch.df)
    entry.inputs = {
        **entry.inputs,
        "ticker": ticker,
        "source": fetch.source,
        "data_fetched_at": fetch.fetched_at,
    }
    return entry


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.resistance_break",
        description="Horizontal-resistance breakout + volume confirm (Secondary 3).",
    )
    p.add_argument("ticker")
    args = p.parse_args()
    emit(compute_from_ticker(args.ticker))


if __name__ == "__main__":
    main()
