"""Batched current-price fetch via yfinance.

Replaces the dead Yahoo `/v7/finance/quote` HTTP endpoint that
scripts/check-positions.ps1 originally used (returns 401 Unauthorized
since Yahoo restricted the free quote API sometime in 2024-2025).

Usage:
    uv run python scripts/fetch_prices.py BABA CEG MRVL [...]

Output (JSON to stdout):
    {"BABA": 129.71, "CEG": 309.75, ...}

Tickers that fail to resolve (delisted, typo, network error) are
silently omitted from the output dict. The caller's contract is
"prices for tickers that yfinance could fetch" — missing entries
mean "no price available," same as the old script's $null return.

Exit code is 0 even on partial failure (e.g. 5 of 7 tickers found);
exit 1 only on argv parse failure. yfinance handles per-ticker
errors internally via its `fast_info` accessor.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    tickers = sys.argv[1:]
    if not tickers:
        json.dump({}, sys.stdout)
        return 0

    import yfinance as yf

    out: dict[str, float] = {}
    data = yf.Tickers(" ".join(tickers))
    for t in tickers:
        try:
            info = data.tickers[t].fast_info
            # fast_info returns a TickerLazyDict; .get() works on it.
            price = (
                info.get("lastPrice")
                or info.get("last_price")
                or info.get("regularMarketPrice")
            )
            if price is not None and float(price) > 0:
                out[t] = float(price)
        except Exception:
            # Silently skip; missing tickers signal "no price available"
            # to the caller, same contract as the old Yahoo HTTP fallback.
            continue

    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
