"""Cross-asset trend — Phase 0 data-reachability probe.

Confirms yfinance (the data_cache provider) reaches the liquid ETF/crypto
proxies for each non-equity asset class, and reports the earliest usable date
+ row count per instrument. This sets the backtest period for the cross-asset
trend validation. Read-only.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import date

from tools.backtest import data_cache

# Candidate cross-asset ETF/crypto proxy universe (trades on equity rails,
# long history, decorrelated-from-US-equity asset classes).
PROBE = {
    "US equity (have)": ["SPY", "QQQ"],
    "Bonds": ["TLT", "IEF", "AGG"],
    "Gold/metals": ["GLD", "SLV"],
    "Broad commodities": ["DBC", "PDBC", "DBA"],
    "Energy": ["USO", "DBO"],
    "USD": ["UUP"],
    "Intl equity": ["EFA", "EEM"],
    "Real estate": ["VNQ"],
    "Crypto": ["BTC-USD", "ETH-USD"],
}

START = "2008-01-01"
END = "2026-05-25"

print(f"{'ticker':<10} {'class':<20} {'first date':<12} {'rows':>7}  status")
for cls, tickers in PROBE.items():
    for t in tickers:
        try:
            data_cache.fetch(t, start=START, end=END, force_refetch=True)
            df = data_cache.load(t)
            first = df.index.min()
            first_s = str(first.date()) if hasattr(first, "date") else str(first)[:10]
            print(f"{t:<10} {cls:<20} {first_s:<12} {len(df):>7}  OK", flush=True)
        except Exception as exc:
            print(f"{t:<10} {cls:<20} {'-':<12} {'-':>7}  FAIL: {exc!r}", flush=True)

print("\nprobe done", flush=True)
