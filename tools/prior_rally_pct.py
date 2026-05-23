"""Prior-rally percent + "neglected" filter for EP setup.

Per ``swing-earnings-pivot.md``: EP candidates must NOT have significantly
rallied over the past 3-6 months — the "neglected → surprise" filter. If a
stock is already extended, the gap is less likely a true repricing event.

The vault notes don't give a sharp numeric threshold; Stockbee/Bonde
material suggests the stock should be base-building or down over the
window. Phase 2 baseline default: ``neglected`` iff both 3m and 6m returns
are ≤ +20%. Threshold is configurable.

CLI::

    uv run python -m tools.prior_rally_pct AAPL
"""
from __future__ import annotations

import argparse

import pandas as pd

from .cli import emit
from .contract import TraceEntry
from .data import fetch_ohlcv

TOOL = "tools/prior_rally_pct.py"

# Phase 2 baseline; refine after walk-forward calibration.
DEFAULT_NEGLECTED_THRESHOLD = 0.20
BARS_3M = 63   # ~63 trading days = 3 months
BARS_6M = 126  # ~126 trading days = 6 months


def _return_pct(close: pd.Series, lookback_bars: int) -> float:
    if len(close) < lookback_bars + 1:
        raise ValueError(
            f"need at least {lookback_bars + 1} bars; got {len(close)}"
        )
    now = float(close.iloc[-1])
    then = float(close.iloc[-(lookback_bars + 1)])
    if then <= 0:
        raise ValueError("non-positive historical close")
    return (now / then) - 1.0


def compute_from_ohlcv(
    df: pd.DataFrame,
    neglected_threshold: float = DEFAULT_NEGLECTED_THRESHOLD,
) -> TraceEntry:
    """Compute 3m + 6m returns + neglected flag.

    Args:
        df: OHLCV DataFrame. Needs ``Close``. Must span at least 127 bars
            (~6 months) so the 6-month lookback is valid.
        neglected_threshold: max return for ``neglected`` to be True.
            Default 0.20 (20%).
    """
    if "Close" not in df.columns:
        raise ValueError("DataFrame missing 'Close' column")
    if len(df) < BARS_6M + 1:
        raise ValueError(f"need at least {BARS_6M + 1} bars; got {len(df)}")
    close = df["Close"].astype(float)
    r3m = _return_pct(close, BARS_3M)
    r6m = _return_pct(close, BARS_6M)
    neglected = (r3m <= neglected_threshold) and (r6m <= neglected_threshold)
    return TraceEntry(
        tool=TOOL,
        inputs={
            "neglected_threshold": neglected_threshold,
            "rows": len(df),
            "last_close_date": str(df.index[-1]),
        },
        output={
            "rally_3m_pct": r3m,
            "rally_6m_pct": r6m,
            "neglected": neglected,
            "neglected_threshold": neglected_threshold,
            "last_close": float(close.iloc[-1]),
        },
    )


def compute_from_ticker(
    ticker: str,
    neglected_threshold: float = DEFAULT_NEGLECTED_THRESHOLD,
) -> TraceEntry:
    fetch = fetch_ohlcv(ticker, period="9mo")
    entry = compute_from_ohlcv(fetch.df, neglected_threshold=neglected_threshold)
    entry.inputs = {
        **entry.inputs,
        "ticker": ticker,
        "source": fetch.source,
        "data_fetched_at": fetch.fetched_at,
    }
    return entry


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.prior_rally_pct",
        description="3m + 6m return % + neglected filter for EP setup.",
    )
    p.add_argument("ticker")
    p.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_NEGLECTED_THRESHOLD,
        help="Max return for 'neglected' to be True. Default 0.20.",
    )
    args = p.parse_args()
    emit(compute_from_ticker(args.ticker, neglected_threshold=args.threshold))


if __name__ == "__main__":
    main()
