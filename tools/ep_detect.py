"""EP detector — gap %, premarket volume, sweet-spot band, intraday expansion.

Per ``swing-earnings-pivot.md``: an EP candidate must have a ≥10% gap up on
big volume from a neglected base. This tool computes the mechanical inputs;
classifying as Super Swan / Swan / Golden / etc. happens in :mod:`tools.ep_grade`.

The agent calls this on a watchlist of pre-market gappers — it does NOT
discover candidates on its own (that's an external scanner concern). Given
a ticker, it returns the gap arithmetic + intraday volume signals so the
caller can decide whether to deep-dive.

CLI::

    uv run python -m tools.ep_detect SMCI
"""
from __future__ import annotations

import argparse

import pandas as pd

from .cli import emit
from .contract import TraceEntry
from .data import fetch_ohlcv

TOOL = "tools/ep_detect.py"

GAP_MIN_PCT = 0.10                  # EP eligibility floor
SWEET_SPOT_MIN_PCT = 0.10           # Golden EP sweet spot lower band
SWEET_SPOT_MAX_PCT = 0.20           # exclusive upper (per swing-earnings-pivot)
LARGE_GAP_PCT = 0.20                # 20%+ = higher Day 1 failure rate
ADV_LOOKBACK = 20


def compute_from_daily_ohlcv(
    daily_df: pd.DataFrame,
    intraday_df: pd.DataFrame | None = None,
) -> TraceEntry:
    """Compute EP signals from daily OHLCV plus optional 1-min intraday.

    Args:
        daily_df: daily OHLCV. Most recent bar is "today" (the EP day);
            second-most-recent bar provides previous close. Must include
            ``Open``, ``Close``, ``Volume`` columns; ``High`` used for
            intraday expansion if intraday_df is absent.
        intraday_df: optional 1-min OHLCV for today, with timestamps.
            Used to compute premarket volume + first-30-min volume.
            If ``None``, those fields are ``None`` and
            ``intraday_data_available`` is False.

    Returns:
        TraceEntry with output keys: ``gap_pct``, ``gap_band``,
        ``ep_eligible``, ``sweet_spot``, ``large_gap_risk``,
        ``intraday_expansion_pct``, ``volume_today_vs_adv``,
        ``premarket_volume_shares``, ``first_30min_volume`` /
        ``first_30min_volume_vs_adv``, ``intraday_data_available``.
    """
    required = {"Open", "Close", "Volume"}
    missing = required - set(daily_df.columns)
    if missing:
        raise ValueError(f"daily_df missing columns: {sorted(missing)}")
    if len(daily_df) < ADV_LOOKBACK + 2:
        raise ValueError(
            f"need at least {ADV_LOOKBACK + 2} daily bars; got {len(daily_df)}"
        )

    prev_close = float(daily_df["Close"].iloc[-2])
    today_open = float(daily_df["Open"].iloc[-1])
    today_high = float(daily_df["High"].iloc[-1]) if "High" in daily_df.columns else today_open
    today_close = float(daily_df["Close"].iloc[-1])
    today_volume = float(daily_df["Volume"].iloc[-1])

    gap_pct = (today_open - prev_close) / prev_close if prev_close > 0 else 0.0
    intraday_expansion_pct = (today_high - today_open) / today_open if today_open > 0 else 0.0

    # 20d ADV excludes today.
    adv_20 = float(daily_df["Volume"].iloc[-(ADV_LOOKBACK + 1) : -1].mean())
    vol_vs_adv = today_volume / adv_20 if adv_20 > 0 else 0.0

    # Gap band per ep_grade.py reference thresholds.
    if gap_pct >= LARGE_GAP_PCT:
        gap_band = "large_20_plus"
    elif SWEET_SPOT_MIN_PCT <= gap_pct < SWEET_SPOT_MAX_PCT:
        gap_band = "sweet_10_to_19"
    elif 0.05 <= gap_pct < 0.10:
        gap_band = "small_5_to_9"
    else:
        gap_band = "below_threshold"

    sweet_spot = gap_band == "sweet_10_to_19"
    large_gap_risk = gap_band == "large_20_plus"
    ep_eligible = gap_pct >= GAP_MIN_PCT

    # Intraday volume signals (best-effort).
    premarket_vol: int | None = None
    first_30min_vol: int | None = None
    first_30min_vs_adv: float | None = None
    intraday_available = False
    if intraday_df is not None and len(intraday_df) > 0 and "Volume" in intraday_df.columns:
        intraday_available = True
        idx = intraday_df.index
        # Premarket: timestamps before 09:30 ET. yfinance returns timezone-aware
        # timestamps; if naive, assume already ET.
        try:
            local = idx.tz_convert("America/New_York") if idx.tz is not None else idx
        except (AttributeError, TypeError):
            local = idx
        times = local.time if hasattr(local, "time") else [ts.time() for ts in local]
        import datetime as _dt

        pre_mask = [t < _dt.time(9, 30) for t in times]
        open_30_mask = [_dt.time(9, 30) <= t < _dt.time(10, 0) for t in times]
        if any(pre_mask):
            premarket_vol = int(intraday_df["Volume"].iloc[pre_mask].sum())
        if any(open_30_mask):
            first_30min_vol = int(intraday_df["Volume"].iloc[open_30_mask].sum())
            if adv_20 > 0:
                first_30min_vs_adv = first_30min_vol / adv_20

    return TraceEntry(
        tool=TOOL,
        inputs={
            "rows": len(daily_df),
            "intraday_rows": len(intraday_df) if intraday_df is not None else 0,
            "last_close_date": str(daily_df.index[-1]),
        },
        output={
            "prev_close": prev_close,
            "today_open": today_open,
            "today_high": today_high,
            "today_close": today_close,
            "gap_pct": gap_pct,
            "gap_band": gap_band,
            "sweet_spot": sweet_spot,
            "large_gap_risk": large_gap_risk,
            "ep_eligible": ep_eligible,
            "intraday_expansion_pct": intraday_expansion_pct,
            "volume_today": today_volume,
            "volume_20d_avg": adv_20,
            "volume_today_vs_adv": vol_vs_adv,
            "premarket_volume_shares": premarket_vol,
            "first_30min_volume": first_30min_vol,
            "first_30min_volume_vs_adv": first_30min_vs_adv,
            "intraday_data_available": intraday_available,
        },
    )


def compute_from_ticker(ticker: str) -> TraceEntry:
    """Fetch daily + (best-effort) 1-min intraday and compute."""
    daily = fetch_ohlcv(ticker, period="3mo", interval="1d")
    intraday = None
    try:
        intraday = fetch_ohlcv(ticker, period="1d", interval="1m")
    except Exception:
        # 1-min data is only available for last 7 days; outside market
        # hours or for inactive tickers it may fail. Best-effort.
        intraday = None
    entry = compute_from_daily_ohlcv(
        daily.df,
        intraday_df=intraday.df if intraday is not None else None,
    )
    entry.inputs = {
        **entry.inputs,
        "ticker": ticker,
        "source_daily": daily.source,
        "daily_fetched_at": daily.fetched_at,
        "source_intraday": intraday.source if intraday is not None else None,
        "intraday_fetched_at": intraday.fetched_at if intraday is not None else None,
    }
    return entry


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.ep_detect",
        description="EP detector: gap %, sweet-spot, intraday signals.",
    )
    p.add_argument("ticker")
    args = p.parse_args()
    emit(compute_from_ticker(args.ticker))


if __name__ == "__main__":
    main()
