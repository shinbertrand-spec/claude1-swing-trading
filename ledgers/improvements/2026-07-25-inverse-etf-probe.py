"""Inverse-ETF reachability probe — synthetic-short exposure on long-only rails.

-1x (non-leveraged) inverse ETFs only: they rise when the underlying falls, so a
long-only trend book buys them to capture DOWNtrends. Leveraged (-2x/-3x) inverse
ETFs are EXCLUDED (daily-reset decay too severe for multi-week holds). Read-only.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.backtest import data_cache

PROBE = {
    "short S&P500 (-1x)": "SH",
    "short Nasdaq100 (-1x)": "PSQ",
    "short Russell2000 (-1x)": "RWM",
    "short 20y Treasury (-1x)": "TBF",
    "short 7-10y Treasury (-1x)": "PST",   # PST is -2x; will note if only lev avail
    "short EAFE (-1x)": "EFZ",
    "short EM (-1x)": "EUM",
    "short gold (-1x)": "DGZ",
    "short oil (-1x)": "SCO",               # SCO is -2x crude; note
}

START = "2008-01-01"
END = "2026-05-25"

print(f"{'ticker':<8} {'class':<28} {'first date':<12} {'rows':>7}  status")
for cls, t in PROBE.items():
    try:
        data_cache.fetch(t, start=START, end=END, force_refetch=True)
        df = data_cache.load(t)
        first = df.index.min()
        first_s = str(first.date()) if hasattr(first, "date") else str(first)[:10]
        print(f"{t:<8} {cls:<28} {first_s:<12} {len(df):>7}  OK", flush=True)
    except Exception as exc:
        print(f"{t:<8} {cls:<28} {'-':<12} {'-':>7}  FAIL: {exc!r}", flush=True)

print("\nprobe done", flush=True)
