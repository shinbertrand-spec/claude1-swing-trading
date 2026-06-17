"""Build the insider buying-events file for the event_insider_buying KIND.

Composes the bulk SEC Form 345 loader (Phase 6a) + the opportunistic classifier
(Phase 3) + the conviction composite (Phase 4) into the events YAML the KIND
replays (Phase 5). Bulk-backed end-to-end → no per-filing EDGAR.

  bulk purchases (form345_bulk.ingest_range)
     → ingest_day_fn (by-date index)            [the event stream]
     → classify_fn (insider history from same bulk data + parquet prices)
     → shares_outstanding_fn (yfinance, best-effort)
     → insider_events.build_events → write_events

Anti-look-ahead is inherited: events keyed off filing_date, tiers scored as-of
the cluster date over realized-window-only history, entry next-bar in the KIND.

Writes the events file (the artifact) + a build-summary line. Run as a
background job; the events file's existence is the result.

CLI::

    uv run python scripts/build_insider_events.py \
        --universe liquid_us_2026q2 --start 2017-01-01 --end 2024-06-30 \
        --out ledgers/insider/events/liquid_us_2026q2.yml
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tools.fundamentals import form345_bulk
from tools.fundamentals.insider_events import build_events, write_events
from tools.fundamentals.insider_track_record import (
    DEFAULT_HORIZON_DAYS,
    compute_track_record,
    default_close_series_fn,
)
from tools.quant_strategies._universe import get_universe


def _shares_outstanding_fn():
    """Best-effort current shares outstanding via yfinance (memoised).

    LIMITATION: current (not point-in-time) shares. For a multi-year backfill
    this over/under-states the % SO of older events; the size bucket is a coarse
    3-way split so the error rarely flips a bucket, but it is a known
    approximation (a historical shares panel would be exact). Falls back to None
    → size bucket UNKNOWN (no size multiplier) on any failure.
    """
    cache: dict[str, float | None] = {}

    def _fn(ticker: str, asof: date):
        if ticker in cache:
            return cache[ticker]
        val = None
        try:
            import yfinance as yf
            fi = yf.Ticker(ticker).fast_info
            for key in ("shares", "shares_outstanding"):
                v = fi.get(key) if hasattr(fi, "get") else getattr(fi, key, None)
                if v:
                    val = float(v)
                    break
        except Exception:  # noqa: BLE001
            val = None
        cache[ticker] = val
        return val
    return _fn


def main() -> int:
    p = argparse.ArgumentParser(prog="build_insider_events")
    p.add_argument("--universe", default="liquid_us_2026q2")
    p.add_argument("--start", default="2017-01-01")
    p.add_argument("--end", default="2024-06-30")
    p.add_argument("--min-conviction", default="medium")
    p.add_argument("--horizon", type=int, default=DEFAULT_HORIZON_DAYS)
    p.add_argument("--price-start", default="2016-07-01")
    p.add_argument("--out", default="ledgers/insider/events/liquid_us_2026q2.yml")
    p.add_argument("--no-shares", action="store_true",
                   help="skip yfinance shares (size bucket = unknown; much faster)")
    args = p.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    universe = [t.upper() for t in get_universe(args.universe)]
    print(f"[build] universe={args.universe} ({len(universe)} tickers) "
          f"range={start}..{end} min_conviction={args.min_conviction}", flush=True)

    # 1. bulk purchases (the event stream + the classifier's history source)
    print("[build] loading bulk Form 345 quarters ...", flush=True)
    all_purchases = form345_bulk.ingest_range(start, end)
    print(f"[build] {len(all_purchases)} open-market purchases loaded", flush=True)

    # by-date index for ingest_day_fn (build_events filters to universe itself)
    by_date: dict[str, list] = defaultdict(list)
    for pp in all_purchases:
        by_date[pp.event_date].append(pp)

    # insider-history index for the classifier (all of an insider's buys)
    history_by_cik: dict[str, list] = defaultdict(list)
    for pp in all_purchases:
        cik = str(getattr(pp, "insider_cik", "") or "")
        if cik:
            history_by_cik[cik].append(pp)

    price_fn = default_close_series_fn(start=args.price_start)

    def classify_fn(cik: str, asof: date):
        return compute_track_record(
            history_by_cik.get(str(cik), []), asof,
            close_series_fn=price_fn, horizon_days=args.horizon, insider_cik=str(cik),
        )

    shares_fn = (lambda t, a: None) if args.no_shares else _shares_outstanding_fn()

    # 2-4. compose → events
    print("[build] composing events (cluster → classify → conviction) ...", flush=True)
    events = build_events(
        universe, start, end,
        ingest_day_fn=lambda d: by_date.get(d, []),
        classify_fn=classify_fn,
        shares_outstanding_fn=shares_fn,
        horizon_days=args.horizon,
        min_conviction=args.min_conviction,
    )

    out = Path(args.out)
    if not out.is_absolute():
        out = _REPO / out
    write_events(out, events, meta={
        "universe": args.universe,
        "range_start": start.isoformat(),
        "range_end": end.isoformat(),
        "min_conviction": args.min_conviction,
        "horizon_days": args.horizon,
        "shares_source": "none" if args.no_shares else "yfinance_current",
    })
    by_level: dict[str, int] = defaultdict(int)
    for e in events:
        by_level[e.conviction_level] += 1
    print(f"[build] DONE: {len(events)} events ({dict(by_level)}) → {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
