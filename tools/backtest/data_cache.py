"""On-disk parquet cache for historical OHLCV.

yfinance is rate-limited and slow; for a 5-year multi-ticker backtest we
fetch once and cache to ``tools/backtest/cache/<ticker>.parquet``. Cache
entries store the fetch timestamp so callers can decide whether to
re-fetch (e.g. live re-validation after market close).

CLI::

    uv run python -m tools.backtest.data_cache fetch AAPL SPY QQQ --start 2020-01-01
    uv run python -m tools.backtest.data_cache info AAPL
    uv run python -m tools.backtest.data_cache clear AAPL
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

CACHE_DIR = Path(__file__).resolve().parent / "cache"
META_SUFFIX = ".meta.txt"


@dataclass
class CacheEntry:
    ticker: str
    path: Path
    rows: int
    start_date: date
    end_date: date
    fetched_at: str
    source: str


def _ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker.upper()}.parquet"


def _meta_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker.upper()}{META_SUFFIX}"


def _write_meta(ticker: str, fetched_at: str, source: str, rows: int) -> None:
    _meta_path(ticker).write_text(
        f"fetched_at={fetched_at}\nsource={source}\nrows={rows}\n",
        encoding="utf-8",
    )


def _read_meta(ticker: str) -> dict[str, str]:
    p = _meta_path(ticker)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def fetch(
    ticker: str,
    start: str | date | None = None,
    end: str | date | None = None,
    force_refetch: bool = False,
) -> CacheEntry:
    """Fetch + cache OHLCV for ``ticker``.

    Args:
        ticker: e.g. ``"AAPL"``.
        start / end: ISO date strings or :class:`date`. Default = last 5y
            ending today.
        force_refetch: if True, ignore existing cache and re-fetch.

    Returns:
        :class:`CacheEntry` pointing at the on-disk parquet.
    """
    _ensure_cache_dir()
    path = _cache_path(ticker)
    if path.exists() and not force_refetch:
        df = pd.read_parquet(path)
        meta = _read_meta(ticker)
        return CacheEntry(
            ticker=ticker.upper(),
            path=path,
            rows=len(df),
            start_date=df.index[0].date(),
            end_date=df.index[-1].date(),
            fetched_at=meta.get("fetched_at", "unknown"),
            source=meta.get("source", "yfinance"),
        )

    import yfinance as yf

    # Default 5y window if not specified.
    if end is None:
        end_d = date.today()
    elif isinstance(end, str):
        end_d = date.fromisoformat(end)
    else:
        end_d = end
    if start is None:
        start_d = date(end_d.year - 5, end_d.month, end_d.day)
    elif isinstance(start, str):
        start_d = date.fromisoformat(start)
    else:
        start_d = start

    df = yf.Ticker(ticker).history(
        start=start_d.isoformat(),
        end=end_d.isoformat(),
        interval="1d",
        auto_adjust=False,
    )
    if df.empty:
        raise RuntimeError(f"yfinance returned no data for {ticker} {start_d}..{end_d}")
    df.to_parquet(path)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_meta(ticker, fetched_at, f"yfinance:{ticker}", len(df))
    return CacheEntry(
        ticker=ticker.upper(),
        path=path,
        rows=len(df),
        start_date=df.index[0].date(),
        end_date=df.index[-1].date(),
        fetched_at=fetched_at,
        source=f"yfinance:{ticker}",
    )


def load(ticker: str) -> pd.DataFrame:
    """Load cached OHLCV for ``ticker``. Raises if not cached."""
    path = _cache_path(ticker)
    if not path.exists():
        raise FileNotFoundError(
            f"No cache for {ticker} at {path}. Run `fetch({ticker!r})` first."
        )
    return pd.read_parquet(path)


def info(ticker: str) -> CacheEntry | None:
    """Return cache metadata for ``ticker``, or None if not cached."""
    path = _cache_path(ticker)
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    meta = _read_meta(ticker)
    return CacheEntry(
        ticker=ticker.upper(),
        path=path,
        rows=len(df),
        start_date=df.index[0].date(),
        end_date=df.index[-1].date(),
        fetched_at=meta.get("fetched_at", "unknown"),
        source=meta.get("source", "yfinance"),
    )


def clear(ticker: str) -> bool:
    """Remove cache for ``ticker``. Returns True if anything was removed."""
    p = _cache_path(ticker)
    m = _meta_path(ticker)
    removed = False
    if p.exists():
        p.unlink()
        removed = True
    if m.exists():
        m.unlink()
        removed = True
    return removed


def main() -> None:
    p = argparse.ArgumentParser(prog="tools.backtest.data_cache")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="Fetch + cache OHLCV for one or more tickers")
    p_fetch.add_argument("tickers", nargs="+")
    p_fetch.add_argument("--start", default=None, help="ISO date, default 5y ago")
    p_fetch.add_argument("--end", default=None, help="ISO date, default today")
    p_fetch.add_argument("--force", action="store_true", help="Re-fetch even if cached")

    p_info = sub.add_parser("info", help="Show cache info for a ticker")
    p_info.add_argument("ticker")

    p_clear = sub.add_parser("clear", help="Remove cache for a ticker")
    p_clear.add_argument("ticker")

    args = p.parse_args()
    if args.cmd == "fetch":
        for t in args.tickers:
            try:
                e = fetch(t, start=args.start, end=args.end, force_refetch=args.force)
                print(
                    f"{e.ticker}: {e.rows} bars {e.start_date}..{e.end_date} "
                    f"cached at {e.path} (fetched {e.fetched_at})"
                )
            except Exception as exc:
                print(f"{t}: FAILED — {exc}")
    elif args.cmd == "info":
        e = info(args.ticker)
        if e is None:
            print(f"{args.ticker.upper()}: not cached")
        else:
            print(
                f"{e.ticker}: {e.rows} bars {e.start_date}..{e.end_date} "
                f"cached at {e.path} (fetched {e.fetched_at}, source {e.source})"
            )
    elif args.cmd == "clear":
        ok = clear(args.ticker)
        print(f"{args.ticker.upper()}: {'removed' if ok else 'no cache to remove'}")


if __name__ == "__main__":
    main()
