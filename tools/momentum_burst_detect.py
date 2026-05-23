"""Momentum Burst detector (per ``swing-momentum-execution.md`` ADD-ON #1).

The Stage-2 trigger for the Anchor-and-Pyramid workflow:

* Daily move ≥ 4% on volume ≥ 1.40× 20-day average
  (OR gap up ≥ 4% with confirming volume)
* Stock must be **above STARTER-day high** when adding (caller verifies)

A Momentum Burst on an existing STARTER position triggers ADD-ON #1 to
bring the position to full intended size.

CLI::

    uv run python -m tools.momentum_burst_detect NVDA
"""
from __future__ import annotations

import argparse

import pandas as pd

from .cli import emit
from .contract import TraceEntry
from .data import fetch_ohlcv

TOOL = "tools/momentum_burst_detect.py"

MOMENTUM_BURST_GAIN_THRESHOLD = 0.04        # 4%+
VOLUME_RATIO_THRESHOLD = 1.40                # 40%+ above 20d avg
GAP_BURST_THRESHOLD = 0.04                   # gap-up alternative
ADV_LOOKBACK = 20


def compute_from_ohlcv(df: pd.DataFrame) -> TraceEntry:
    """Detect a Momentum Burst on the most recent bar."""
    required = {"Open", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")
    if len(df) < ADV_LOOKBACK + 2:
        raise ValueError(f"need at least {ADV_LOOKBACK + 2} bars; got {len(df)}")

    close = df["Close"].astype(float)
    open_ = df["Open"].astype(float)
    volume = df["Volume"].astype(float)

    today_close = float(close.iloc[-1])
    today_open = float(open_.iloc[-1])
    yest_close = float(close.iloc[-2])

    day_pct = (today_close / yest_close - 1.0) if yest_close > 0 else 0.0
    gap_pct = (today_open / yest_close - 1.0) if yest_close > 0 else 0.0
    today_volume = float(volume.iloc[-1])
    adv_20 = float(volume.iloc[-(ADV_LOOKBACK + 1) : -1].mean())
    vol_ratio = today_volume / adv_20 if adv_20 > 0 else 0.0

    daily_burst = (day_pct >= MOMENTUM_BURST_GAIN_THRESHOLD) and (
        vol_ratio >= VOLUME_RATIO_THRESHOLD
    )
    gap_burst = (gap_pct >= GAP_BURST_THRESHOLD) and (
        vol_ratio >= VOLUME_RATIO_THRESHOLD
    )
    triggered = daily_burst or gap_burst

    return TraceEntry(
        tool=TOOL,
        inputs={
            "rows": len(df),
            "last_close_date": str(df.index[-1]),
        },
        output={
            "triggered": triggered,
            "daily_burst": daily_burst,
            "gap_burst": gap_burst,
            "day_pct": day_pct,
            "gap_pct": gap_pct,
            "volume_today": today_volume,
            "volume_20d_avg": adv_20,
            "volume_ratio": vol_ratio,
        },
    )


def compute_from_ticker(ticker: str) -> TraceEntry:
    fetch = fetch_ohlcv(ticker, period="3mo")
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
        prog="tools.momentum_burst_detect",
        description="Detect Momentum Burst (4%+ on 40%+ volume).",
    )
    p.add_argument("ticker")
    args = p.parse_args()
    emit(compute_from_ticker(args.ticker))


if __name__ == "__main__":
    main()
