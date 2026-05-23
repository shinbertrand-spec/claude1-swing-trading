"""Pullback-to-20-day-SMA detector (per ``swing-setup-library.md`` Secondary 1).

Criteria (all must pass):

* Stock in Stage 2 (caller verifies via :mod:`tools.trend_template`)
* Today's price within ``proximity_pct`` of the 20-day SMA (default 1%)
* Volume on pullback declining vs 20-day average
* Bullish reversal candle today: hammer (long lower wick, small body, close near high)
  OR bullish engulfing (today's body engulfs prior down-day body)

Per swing-setup-library: "lower confidence than VCP; max position size = 60%
of SEPA setup."

CLI::

    uv run python -m tools.pullback_detect MSFT
"""
from __future__ import annotations

import argparse

import pandas as pd

from .cli import emit
from .contract import TraceEntry
from .data import fetch_ohlcv

TOOL = "tools/pullback_detect.py"

MA_PERIOD = 20
DEFAULT_PROXIMITY_PCT = 1.0       # within 1% of 20-day SMA
DECLINING_VOL_RATIO = 1.0         # vol_ratio < 1.0 = declining (today vol below 20d avg)

# Reversal-candle heuristics.
HAMMER_LOWER_WICK_MIN_RATIO = 2.0  # lower wick >= 2× body
HAMMER_UPPER_WICK_MAX_RATIO = 0.5  # upper wick <= 0.5× body
HAMMER_BODY_MAX_RANGE_RATIO = 0.35  # body <= 35% of full range


def _is_hammer(open_: float, high: float, low: float, close: float) -> bool:
    body = abs(close - open_)
    if body == 0:
        return False
    rng = high - low
    if rng == 0:
        return False
    upper_wick = high - max(open_, close)
    lower_wick = min(open_, close) - low
    return (
        lower_wick >= HAMMER_LOWER_WICK_MIN_RATIO * body
        and upper_wick <= HAMMER_UPPER_WICK_MAX_RATIO * body
        and body <= HAMMER_BODY_MAX_RANGE_RATIO * rng
    )


def _is_bullish_engulfing(
    prior_open: float, prior_close: float, today_open: float, today_close: float
) -> bool:
    """Bullish engulfing: prior bar was red, today's bar is green AND
    today's body fully contains prior body."""
    prior_red = prior_close < prior_open
    today_green = today_close > today_open
    if not (prior_red and today_green):
        return False
    return today_open <= prior_close and today_close >= prior_open


def compute_from_ohlcv(
    df: pd.DataFrame,
    proximity_pct: float = DEFAULT_PROXIMITY_PCT,
) -> TraceEntry:
    """Evaluate Secondary-1 pullback criteria on the most recent bar.

    Args:
        df: OHLCV DataFrame. Needs Open, High, Low, Close, Volume.
        proximity_pct: how close to the 20-day SMA today's price must be
            (as a percent of price). Default 1.0 (within 1%).
    """
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")
    if len(df) < MA_PERIOD + 2:
        raise ValueError(f"need at least {MA_PERIOD + 2} bars; got {len(df)}")

    close = df["Close"].astype(float)
    open_ = df["Open"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    volume = df["Volume"].astype(float)

    today_close = float(close.iloc[-1])
    today_open = float(open_.iloc[-1])
    today_high = float(high.iloc[-1])
    today_low = float(low.iloc[-1])
    prior_open = float(open_.iloc[-2])
    prior_close = float(close.iloc[-2])

    sma_20 = float(close.tail(MA_PERIOD).mean())
    distance_pct = abs(today_close - sma_20) / today_close * 100.0
    near_20sma = distance_pct <= proximity_pct

    today_vol = float(volume.iloc[-1])
    avg_vol_20 = float(volume.iloc[-(MA_PERIOD + 1) : -1].mean())
    vol_ratio = today_vol / avg_vol_20 if avg_vol_20 > 0 else 0.0
    declining_volume = vol_ratio < DECLINING_VOL_RATIO

    hammer = _is_hammer(today_open, today_high, today_low, today_close)
    bullish_engulfing = _is_bullish_engulfing(
        prior_open, prior_close, today_open, today_close
    )
    reversal_candle = hammer or bullish_engulfing

    criteria = {
        "near_20sma": near_20sma,
        "declining_volume_on_pullback": declining_volume,
        "bullish_reversal_candle": reversal_candle,
    }
    detected = all(criteria.values())

    # Stop placement guidance: below the reversal candle low.
    stop_price = today_low - 0.05 if reversal_candle else None

    return TraceEntry(
        tool=TOOL,
        inputs={
            "proximity_pct": proximity_pct,
            "rows": len(df),
            "last_close_date": str(df.index[-1]),
        },
        output={
            "detected": detected,
            "criteria": criteria,
            "candle_type": (
                "hammer" if hammer else "bullish_engulfing" if bullish_engulfing else None
            ),
            "stats": {
                "today_close": today_close,
                "sma_20": sma_20,
                "distance_pct": distance_pct,
                "volume_ratio_today_vs_20d": vol_ratio,
            },
            "suggested_stop": stop_price,
        },
    )


def compute_from_ticker(
    ticker: str, proximity_pct: float = DEFAULT_PROXIMITY_PCT
) -> TraceEntry:
    fetch = fetch_ohlcv(ticker, period="6mo")
    entry = compute_from_ohlcv(fetch.df, proximity_pct=proximity_pct)
    entry.inputs = {
        **entry.inputs,
        "ticker": ticker,
        "source": fetch.source,
        "data_fetched_at": fetch.fetched_at,
    }
    return entry


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.pullback_detect",
        description="Pullback to 20-day SMA + bullish reversal candle (Secondary 1).",
    )
    p.add_argument("ticker")
    p.add_argument("--proximity-pct", type=float, default=DEFAULT_PROXIMITY_PCT)
    args = p.parse_args()
    emit(compute_from_ticker(args.ticker, proximity_pct=args.proximity_pct))


if __name__ == "__main__":
    main()
