"""Stockbee Low-Threshold Breakout (SLTB) scanner.

Per ``swing-momentum-execution.md`` (Bonde Anchor-and-Pyramid Stage 1):

Bullish SLTB criteria (all must pass):

* 7-day MA at least 5% above 65-day MA (established uptrend)
* Yesterday's gain < 2% (tight prior consolidation)
* Today's % gain > yesterday's % gain (acceleration)
* Close above open AND above prior close
* 3-day min volume ≥ 100K shares (liquidity)
* Close ≥ $3 (no penny stocks)

A SLTB hit triggers STARTER entry at 1/3 intended size.

CLI::

    uv run python -m tools.sltb_scan NVDA
"""
from __future__ import annotations

import argparse

import pandas as pd

from .cli import emit
from .contract import TraceEntry
from .data import fetch_ohlcv

TOOL = "tools/sltb_scan.py"

# Per swing-momentum-execution.md (Bonde TC2000 formulas translated).
MA_SHORT = 7
MA_LONG = 65
TREND_MARGIN = 0.05               # 7-day MA >= 5% above 65-day MA
YESTERDAY_GAIN_MAX = 0.02         # < 2%
MIN_3D_VOLUME = 100_000
MIN_CLOSE_PRICE = 3.0


def compute_from_ohlcv(df: pd.DataFrame) -> TraceEntry:
    """Evaluate the 6 SLTB-bullish criteria on the last bar of ``df``.

    Args:
        df: OHLCV DataFrame indexed by date. Needs Open, Close, Volume.
            Must have at least ``MA_LONG + 1`` bars.
    """
    required = {"Open", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")
    if len(df) < MA_LONG + 1:
        raise ValueError(f"need at least {MA_LONG + 1} bars; got {len(df)}")

    close = df["Close"].astype(float)
    open_ = df["Open"].astype(float)
    volume = df["Volume"].astype(float)

    today_close = float(close.iloc[-1])
    today_open = float(open_.iloc[-1])
    yest_close = float(close.iloc[-2])
    prev_yest_close = float(close.iloc[-3])

    ma_short = float(close.tail(MA_SHORT).mean())
    ma_long = float(close.tail(MA_LONG).mean())
    trend_ratio = (ma_short / ma_long - 1.0) if ma_long > 0 else 0.0

    today_gain = (today_close / yest_close - 1.0) if yest_close > 0 else 0.0
    yest_gain = (yest_close / prev_yest_close - 1.0) if prev_yest_close > 0 else 0.0

    min_3d_vol = float(volume.tail(3).min())

    criteria = {
        "trend_ma7_5pct_above_ma65": trend_ratio >= TREND_MARGIN,
        "yesterday_gain_under_2pct": yest_gain < YESTERDAY_GAIN_MAX,
        "today_gain_exceeds_yesterday": today_gain > yest_gain,
        "close_above_open_and_prior_close": (today_close > today_open) and (today_close > yest_close),
        "min_3d_volume_100k": min_3d_vol >= MIN_3D_VOLUME,
        "close_at_least_3_dollars": today_close >= MIN_CLOSE_PRICE,
    }
    triggered = all(criteria.values())

    return TraceEntry(
        tool=TOOL,
        inputs={
            "ma_short": MA_SHORT,
            "ma_long": MA_LONG,
            "rows": len(df),
            "last_close_date": str(df.index[-1]),
        },
        output={
            "sltb_triggered": triggered,
            "criteria": criteria,
            "stats": {
                "ma_7": ma_short,
                "ma_65": ma_long,
                "trend_ratio": trend_ratio,
                "today_gain": today_gain,
                "yesterday_gain": yest_gain,
                "today_close": today_close,
                "today_open": today_open,
                "min_3d_volume": min_3d_vol,
            },
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
        prog="tools.sltb_scan",
        description="Stockbee Low-Threshold Breakout scan (bullish).",
    )
    p.add_argument("ticker")
    args = p.parse_args()
    emit(compute_from_ticker(args.ticker))


if __name__ == "__main__":
    main()
