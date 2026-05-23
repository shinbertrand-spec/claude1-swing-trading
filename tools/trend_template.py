"""Minervini 8-point Trend Template (per ``swing-regime-playbook.md``).

The 8 criteria:

1. Price > 150d SMA AND > 200d SMA
2. 150d SMA > 200d SMA
3. 200d SMA trending up (higher than 30 days ago)
4. 50d SMA > 150d SMA AND > 200d SMA
5. Price > 50d SMA
6. Price ≥ 30% above 52-week low
7. Price within 25% of 52-week high
8. RS rating ≥ 70 (proxy: 12-month return vs SPY percentile)

Criterion 8 is **skipped for indices** (broad-market regime check) — pass
``include_rs=False`` and the result reports score out of 7.

Stage classification derives from the same data:
    Stage 2 — passes ≥ 6
    Stage 4 — price < 200d MA AND 200d MA falling
    Stage 3 — price > 200d MA but 200d MA not rising
    Stage 1 — otherwise (typically below 200d but base not yet broken down)

CLI::

    uv run python -m tools.trend_template AAPL
"""
from __future__ import annotations

import argparse

import pandas as pd

from .cli import emit
from .contract import TraceEntry
from .data import fetch_ohlcv

TOOL = "tools/trend_template.py"


def _sma(series: pd.Series, n: int) -> float:
    return float(series.tail(n).mean())


def _sma_n_days_ago(series: pd.Series, n: int, lookback: int) -> float:
    """SMA(n) computed as of `lookback` bars ago."""
    return float(series.iloc[-(lookback + 1) - n + 1 : -lookback].mean())


def compute_from_ohlcv(
    df: pd.DataFrame,
    include_rs: bool = True,
    rs_rating: int | None = None,
) -> TraceEntry:
    """Run the 8 criteria against an OHLCV DataFrame.

    Args:
        df: OHLCV DataFrame indexed by date. Must contain ``Close``;
            ``High`` / ``Low`` only used for 52w distance if present
            (else falls back to Close).
        include_rs: whether criterion 8 (RS rating) counts. For indices,
            pass False — playbook says skip.
        rs_rating: 1-99 IBD-style rating, if known. If None and
            ``include_rs`` is True, criterion 8 is reported as
            ``unknown`` (counts as False).

    Raises:
        ValueError: if fewer than 252 bars of data (one trading year).
    """
    if "Close" not in df.columns:
        raise ValueError("DataFrame missing 'Close' column")
    if len(df) < 252:
        raise ValueError(
            f"need >= 252 bars (one trading year) for trend template; got {len(df)}"
        )

    close = df["Close"].astype(float)
    high_series = df["High"].astype(float) if "High" in df.columns else close
    low_series = df["Low"].astype(float) if "Low" in df.columns else close

    price = float(close.iloc[-1])
    sma_50 = _sma(close, 50)
    sma_150 = _sma(close, 150)
    sma_200 = _sma(close, 200)
    # 200d MA value 30 bars ago — for criterion 3 (trending up).
    sma_200_30d_ago = _sma_n_days_ago(close, 200, lookback=30)

    high_52w = float(high_series.tail(252).max())
    low_52w = float(low_series.tail(252).min())
    pct_above_52w_low = (price / low_52w - 1.0) * 100.0 if low_52w > 0 else 0.0
    pct_below_52w_high = (1.0 - price / high_52w) * 100.0 if high_52w > 0 else 0.0

    criteria: dict[str, bool] = {
        "c1_price_above_150_and_200_sma": (price > sma_150) and (price > sma_200),
        "c2_sma150_above_sma200": sma_150 > sma_200,
        "c3_sma200_rising_30d": sma_200 > sma_200_30d_ago,
        "c4_sma50_above_150_and_200": (sma_50 > sma_150) and (sma_50 > sma_200),
        "c5_price_above_sma50": price > sma_50,
        "c6_price_30pct_above_52w_low": pct_above_52w_low >= 30.0,
        "c7_price_within_25pct_of_52w_high": pct_below_52w_high <= 25.0,
    }
    if include_rs:
        if rs_rating is None:
            criteria["c8_rs_rating_ge_70"] = False  # unknown counts as False
            rs_status = "unknown"
        else:
            criteria["c8_rs_rating_ge_70"] = rs_rating >= 70
            rs_status = "known"
    else:
        rs_status = "skipped_for_index"

    passes = sum(criteria.values())
    total = len(criteria)

    # Stage classification (derived).
    # Stage 2 requires not just MA alignment but evidence of an actual advance
    # from the base (c6: >= 30% above 52w low). Without c6 the data is flat —
    # by Weinstein's definition that's Stage 1 (basing), not Stage 2 (advance).
    stage_2_evidence = (
        passes >= 6
        and criteria["c1_price_above_150_and_200_sma"]
        and criteria["c3_sma200_rising_30d"]
        and criteria["c6_price_30pct_above_52w_low"]
    )
    if stage_2_evidence:
        stage = 2
    elif price < sma_200 and sma_200 < sma_200_30d_ago:
        stage = 4
    elif price >= sma_200 and not criteria["c3_sma200_rising_30d"]:
        stage = 3
    else:
        stage = 1

    return TraceEntry(
        tool=TOOL,
        inputs={
            "include_rs": include_rs,
            "rs_rating": rs_rating,
            "rows": len(df),
            "last_close_date": str(df.index[-1]),
        },
        output={
            "trend_template_passes": passes,
            "trend_template_total": total,
            "criteria": criteria,
            "stage": stage,
            "rs_status": rs_status,
            "stats": {
                "price": price,
                "sma_50": sma_50,
                "sma_150": sma_150,
                "sma_200": sma_200,
                "sma_200_30d_ago": sma_200_30d_ago,
                "high_52w": high_52w,
                "low_52w": low_52w,
                "pct_above_52w_low": pct_above_52w_low,
                "pct_below_52w_high": pct_below_52w_high,
            },
        },
    )


def compute_from_ticker(
    ticker: str,
    include_rs: bool = True,
    rs_rating: int | None = None,
) -> TraceEntry:
    fetch = fetch_ohlcv(ticker, period="2y")  # 2y so the 200d-30d-ago lookup has headroom
    entry = compute_from_ohlcv(fetch.df, include_rs=include_rs, rs_rating=rs_rating)
    entry.inputs = {
        **entry.inputs,
        "ticker": ticker,
        "source": fetch.source,
        "data_fetched_at": fetch.fetched_at,
    }
    return entry


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.trend_template",
        description="Minervini 8-point Trend Template + stage classification.",
    )
    p.add_argument("ticker")
    p.add_argument("--no-rs", action="store_true", help="Skip RS criterion (use for indices)")
    p.add_argument("--rs-rating", type=int, default=None, help="Known IBD RS rating 1-99")
    args = p.parse_args()
    emit(
        compute_from_ticker(
            args.ticker,
            include_rs=not args.no_rs,
            rs_rating=args.rs_rating,
        )
    )


if __name__ == "__main__":
    main()
