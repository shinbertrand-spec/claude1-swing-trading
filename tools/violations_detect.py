"""Breakout-violation detector (per ``swing-sell-discipline.md`` Trigger #2).

# v1-preliminary: revisit after Minervini book v2 ingestion

Counts how many of 5 post-entry violations are firing:

1. ``volume_asymmetry`` — recent average up-day volume LESS than average
   down-day volume (sellers winning the volume tape)
2. ``three_lower_lows_on_volume`` — 3 consecutive lower lows AND each
   accompanied by above-average volume
3. ``more_down_than_up`` — since entry, more down days than up days
4. ``more_bad_closes_than_good`` — more closes in the bottom half of the
   daily range than in the top half
5. ``close_below_20_or_50_MA_on_heavy_volume`` — today's close < 20-day MA
   OR < 50-day MA AND volume ≥ 1.5× 20-day average

Violation #5 alone = full exit, per the decision matrix in
``sell_decision.py``.

CLI::

    uv run python -m tools.violations_detect NVDA --entry-date 2026-05-17
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

import pandas as pd

from .cli import emit
from .contract import TraceEntry
from .data import fetch_ohlcv

TOOL = "tools/violations_detect.py"

ASYMMETRY_LOOKBACK = 10
THREE_LOWER_LOWS_BARS = 3
MA_SHORT = 20
MA_LONG = 50
HEAVY_VOL_RATIO = 1.5


def _to_date(d: str | date | datetime) -> date:
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    return datetime.fromisoformat(str(d)).date()


def compute_from_ohlcv(
    df: pd.DataFrame,
    entry_date: str | date,
) -> TraceEntry:
    """Evaluate the 5 violations on the most recent bar relative to entry.

    Args:
        df: OHLCV DataFrame. Needs Open, Close, Volume, High, Low.
        entry_date: ISO date of position entry.
    """
    required = {"Open", "Close", "Volume", "High", "Low"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")
    if len(df) < MA_LONG + 1:
        raise ValueError(f"need at least {MA_LONG + 1} bars; got {len(df)}")
    entry_d = _to_date(entry_date)
    idx_dates = [pd.Timestamp(ts).date() for ts in df.index]
    if entry_d not in idx_dates:
        raise ValueError(
            f"entry_date {entry_d} not in DataFrame index "
            f"(span: {idx_dates[0]} → {idx_dates[-1]})"
        )
    entry_pos = idx_dates.index(entry_d)

    close = df["Close"].astype(float)
    open_ = df["Open"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    volume = df["Volume"].astype(float)

    # Violation 1 — volume asymmetry over last ASYMMETRY_LOOKBACK bars.
    recent = df.iloc[-ASYMMETRY_LOOKBACK:]
    diffs = recent["Close"].diff().iloc[1:]
    up_vols = recent["Volume"].iloc[1:][diffs > 0]
    down_vols = recent["Volume"].iloc[1:][diffs < 0]
    if len(up_vols) > 0 and len(down_vols) > 0:
        volume_asymmetry = float(down_vols.mean()) > float(up_vols.mean())
    else:
        volume_asymmetry = False

    # Violation 2 — 3 consecutive lower lows AND each on above-avg volume.
    last_low_window = low.iloc[-(THREE_LOWER_LOWS_BARS + 1):]
    diffs_lows = last_low_window.diff().iloc[1:]
    three_lower_lows = all(d < 0 for d in diffs_lows)
    avg_vol = float(volume.tail(20).mean())
    recent_vols_above_avg = all(v > avg_vol for v in volume.iloc[-THREE_LOWER_LOWS_BARS:])
    three_lower_lows_on_vol = three_lower_lows and recent_vols_above_avg

    # Violation 3 — more down days than up days since entry.
    post_entry = close.iloc[entry_pos + 1 :]
    if len(post_entry) >= 2:
        post_diffs = post_entry.diff().iloc[1:]
        up_count = int((post_diffs > 0).sum())
        down_count = int((post_diffs < 0).sum())
        more_down_than_up = down_count > up_count
    else:
        more_down_than_up = False

    # Violation 4 — more bad closes than good (bottom half of daily range vs top).
    post_open = open_.iloc[entry_pos + 1 :]
    post_close = close.iloc[entry_pos + 1 :]
    post_high = high.iloc[entry_pos + 1 :]
    post_low = low.iloc[entry_pos + 1 :]
    if len(post_close) >= 2:
        midpoints = (post_high + post_low) / 2.0
        good_closes = int((post_close > midpoints).sum())
        bad_closes = int((post_close < midpoints).sum())
        more_bad_than_good = bad_closes > good_closes
    else:
        more_bad_than_good = False

    # Violation 5 — close below 20- or 50-MA AND heavy volume.
    today_close = float(close.iloc[-1])
    today_volume = float(volume.iloc[-1])
    ma20 = float(close.tail(MA_SHORT).mean())
    ma50 = float(close.tail(MA_LONG).mean())
    adv_20 = float(volume.iloc[-(MA_SHORT + 1) : -1].mean())
    heavy_vol = today_volume >= HEAVY_VOL_RATIO * adv_20 if adv_20 > 0 else False
    close_below_ma = (today_close < ma20) or (today_close < ma50)
    violation_5 = close_below_ma and heavy_vol

    violations = {
        "volume_asymmetry": volume_asymmetry,
        "three_lower_lows_on_volume": three_lower_lows_on_vol,
        "more_down_than_up": more_down_than_up,
        "more_bad_closes_than_good": more_bad_than_good,
        "close_below_20_or_50_MA_on_heavy_volume": violation_5,
    }
    firing_count = sum(1 for v in violations.values() if v)

    return TraceEntry(
        tool=TOOL,
        inputs={
            "entry_date": entry_d.isoformat(),
            "rows": len(df),
            "last_close_date": str(df.index[-1]),
            "v1_preliminary": True,
        },
        output={
            "violations_firing": firing_count,
            "violations": violations,
            "violation_5_alone_full_exit": violation_5,
            "stats": {
                "ma_20": ma20,
                "ma_50": ma50,
                "today_close": today_close,
                "today_volume": today_volume,
                "volume_20d_avg": adv_20,
                "heavy_volume": heavy_vol,
            },
            "v1_preliminary_flag": True,
        },
    )


def compute_from_ticker(ticker: str, entry_date: str | date) -> TraceEntry:
    fetch = fetch_ohlcv(ticker, period="6mo")
    entry = compute_from_ohlcv(fetch.df, entry_date=entry_date)
    entry.inputs = {
        **entry.inputs,
        "ticker": ticker,
        "source": fetch.source,
        "data_fetched_at": fetch.fetched_at,
    }
    return entry


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.violations_detect",
        description="Count 5 breakout-violation patterns firing. v1-preliminary.",
    )
    p.add_argument("ticker")
    p.add_argument("--entry-date", required=True)
    args = p.parse_args()
    emit(compute_from_ticker(args.ticker, entry_date=args.entry_date))


if __name__ == "__main__":
    main()
