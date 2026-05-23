"""Thin wrapper around yfinance with timestamped output.

Per Requirement 4: every fetched datum carries a ``fetched_at``. Returns
:class:`DataFetchResult` so downstream tools can populate ledger sections
truthfully without re-derivation.

The wrapper is intentionally minimal — yfinance is the Phase 2 default;
:func:`fetch_ohlcv` is the only abstraction point if/when we add Alpaca or
IBKR later.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class DataFetchResult:
    """OHLCV DataFrame plus provenance."""

    df: pd.DataFrame
    fetched_at: str
    source: str
    ticker: str
    period: str
    interval: str


def fetch_ohlcv(
    ticker: str,
    period: str = "1y",
    interval: str = "1d",
) -> DataFetchResult:
    """Fetch OHLCV via yfinance. ``period``: ``1y``/``6mo``/``3mo``/etc."""
    import yfinance as yf

    df = yf.Ticker(ticker).history(
        period=period, interval=interval, auto_adjust=False
    )
    if df.empty:
        raise RuntimeError(
            f"No OHLCV data returned for {ticker} period={period} interval={interval}"
        )
    return DataFetchResult(
        df=df,
        fetched_at=_utc_now_iso(),
        source=f"yfinance:{ticker}",
        ticker=ticker,
        period=period,
        interval=interval,
    )
