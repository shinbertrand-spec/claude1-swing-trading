"""Compute Wilder's Average True Range (ATR) on an OHLCV DataFrame.

ATR is the input to ``stop_sizer`` and ``position_sizer``. Per
``swing-position-sizing.md``: the agent must never re-derive ATR — it calls
this tool. Per Requirement 2 (Liar Circuits).

Definition (Wilder, 1978):
    TR_t  = max(H_t - L_t,  |H_t - C_{t-1}|,  |L_t - C_{t-1}|)
    ATR_n = mean(TR_1..n)                              # seed
    ATR_t = ((ATR_{t-1} * (n - 1)) + TR_t) / n         # for t > n

CLI::

    uv run python -m tools.atr_compute AAPL --period 14
"""
from __future__ import annotations

import argparse

import pandas as pd

from .cli import emit
from .contract import TraceEntry
from .data import fetch_ohlcv

TOOL = "tools/atr_compute.py"
DEFAULT_PERIOD = 14


def compute_from_ohlcv(df: pd.DataFrame, period: int = DEFAULT_PERIOD) -> TraceEntry:
    """Compute ATR on a DataFrame with columns High/Low/Close.

    Args:
        df: OHLCV DataFrame indexed by date. Must contain ``High``,
            ``Low``, ``Close`` columns.
        period: smoothing length. Defaults to 14 (Wilder).

    Raises:
        ValueError: if ``df`` lacks required columns or has fewer than
            ``period + 1`` rows.
    """
    required = {"High", "Low", "Close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing required columns: {sorted(missing)}")
    if len(df) < period + 1:
        raise ValueError(
            f"need at least {period + 1} rows for ATR({period}); got {len(df)}"
        )

    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Wilder smoothing: seed = simple mean over first `period`, then EMA-style.
    atr = pd.Series(index=tr.index, dtype=float)
    seed_block = tr.iloc[1 : period + 1]
    atr.iloc[period] = seed_block.mean()
    for i in range(period + 1, len(tr)):
        atr.iloc[i] = (atr.iloc[i - 1] * (period - 1) + tr.iloc[i]) / period

    last_atr = float(atr.iloc[-1])
    last_date = df.index[-1]
    last_close = float(close.iloc[-1])

    return TraceEntry(
        tool=TOOL,
        inputs={"period": period, "rows": len(df), "last_close_date": str(last_date)},
        output={
            "atr": last_atr,
            "atr_pct_of_close": (last_atr / last_close) if last_close else None,
            "period": period,
            "last_close": last_close,
            "last_close_date": str(last_date),
        },
    )


def compute_from_ticker(ticker: str, period: int = DEFAULT_PERIOD) -> TraceEntry:
    """Fetch via yfinance and compute ATR. Augments inputs with provenance."""
    fetch = fetch_ohlcv(ticker, period="6mo")
    entry = compute_from_ohlcv(fetch.df, period=period)
    entry.inputs = {
        **entry.inputs,
        "ticker": ticker,
        "source": fetch.source,
        "data_fetched_at": fetch.fetched_at,
    }
    return entry


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.atr_compute",
        description="Compute Wilder ATR for a ticker.",
    )
    p.add_argument("ticker")
    p.add_argument("--period", type=int, default=DEFAULT_PERIOD)
    args = p.parse_args()
    emit(compute_from_ticker(args.ticker, period=args.period))


if __name__ == "__main__":
    main()
