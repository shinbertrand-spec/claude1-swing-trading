"""One-off: force-refetch a registered universe from 2017-01-01.

Extends the cached OHLCV history so rolling-walk-forward backtests can
cover COVID 2020 + mania 2021 + rate-hike 2022 in addition to 2023-2025.

Usage:
    uv run python scripts/refetch_universe_2017.py                       # default: sp500_leaning_88
    uv run python scripts/refetch_universe_2017.py --universe sp500_2026q2
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.backtest import data_cache
from tools.quant_strategies._universe import get_universe


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--universe",
        default="sp500_leaning_88",
        help="Registered universe name (see tools/quant_strategies/_universes/).",
    )
    p.add_argument("--start", default="2017-01-01")
    p.add_argument("--end", default="2026-05-25")
    args = p.parse_args()

    tickers = get_universe(args.universe)
    if "SPY" not in tickers:
        tickers.append("SPY")

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    print(f"# Refetching universe {args.universe!r}: {len(tickers)} tickers, "
          f"{args.start}..{args.end}", flush=True)

    ok, fail = [], []
    for i, t in enumerate(tickers, 1):
        try:
            e = data_cache.fetch(t, start=start, end=end, force_refetch=True)
            ok.append((t, e.rows, e.start_date, e.end_date))
            if i % 20 == 0:
                print(f"  ... {i}/{len(tickers)} fetched", flush=True)
        except Exception as ex:
            fail.append((t, str(ex)[:80]))

    print(f"OK: {len(ok)}  FAIL: {len(fail)}")
    for t, err in fail:
        print(f"  FAIL {t}: {err}")
    # First-trade dates: which tickers have shorter histories than full 2017-2025?
    short = [(t, sd) for t, _, sd, _ in ok if sd > date(2017, 6, 30)]
    print(f"\nTickers with first-trade after 2017-06-30 (n={len(short)}):")
    for t, sd in sorted(short, key=lambda x: x[1]):
        print(f"  {t:6s}  first={sd}")


if __name__ == "__main__":
    main()
