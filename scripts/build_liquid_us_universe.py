"""Build the ``liquid_us_2026q2`` registered universe.

Pipeline:

1. Scrape Wikipedia's S&P 400 (MidCap) and S&P 600 (SmallCap) constituent
   lists — same source pattern used to build sp500_2026q2. Together with
   the S&P 500 base this is the S&P 1500 (~1500 names), which is a stable,
   reproducible proxy for the liquid US large/mid/small-cap universe.
2. Union with the existing sp500_2026q2 universe (the S&P 500 base).
3. For each candidate, fetch a year of OHLCV via tools.backtest.data_cache
   (re-uses cache; only network-fetches the new tickers).
4. Compute trailing-60d median daily share volume. Filter to median >= 500K.
5. Write the result as ``tools/quant_strategies/_universes/liquid_us_2026q2.yml``.

Usage::

    uv run python scripts/build_liquid_us_universe.py

Re-run quarterly when refreshing the universe.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.backtest import data_cache
from tools.quant_strategies._universe import get_universe


WIKIPEDIA_SP_PAGES = {
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "sp600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}

OUT_PATH = (
    Path(__file__).resolve().parent.parent
    / "tools/quant_strategies/_universes/liquid_us_2026q2.yml"
)

ADV_FLOOR_SHARES = 500_000
ADV_LOOKBACK_DAYS = 60


# First-column ticker pattern in the wikitable. Tickers are wrapped in an
# external <a> link to nyse.com or nasdaq.com. Some rows have a leading
# <span class="anchor">; the ticker may use a dot (e.g. BRK.B) which we
# normalise to a hyphen for yfinance compatibility.
_TICKER_PATTERN = re.compile(
    r'<a[^>]+class="external text"[^>]*>([A-Z][A-Z0-9.\-]{0,5})</a>'
)


def fetch_sp_constituents(label: str, url: str) -> list[str]:
    """Scrape a Wikipedia 'List of S&P X companies' page for tickers.

    The first <table class="wikitable"> on the page is the constituents
    table. Column ordering differs across pages: S&P 500 + S&P 600 put the
    ticker in the first <td>; S&P 400 puts the GICS sector first and the
    ticker in a later column. To handle both, we scan ALL <td>s in each
    row and accept the first one containing an external <a> link whose
    text matches the ticker pattern (1-6 chars of [A-Z], optional . or -).
    Returns the deduped ticker list, or [] on fetch failure.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "claude1/1.0 (research)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"WARN: {label} Wikipedia fetch failed ({e})", flush=True)
        return []

    table_match = re.search(
        r'<table[^>]*class="[^"]*wikitable[^"]*"[^>]*>([\s\S]*?)</table>',
        raw,
    )
    if not table_match:
        print(f"WARN: {label} no wikitable found", flush=True)
        return []
    table_body = table_match.group(1)
    rows = re.findall(r'<tr>([\s\S]*?)</tr>', table_body)

    out: list[str] = []
    for row in rows:
        # Scan every <td ...> in the row (attributes tolerated — S&P 400 uses
        # style="border-color:inherit;" on cells); accept first ticker match.
        for td in re.findall(r'<td[^>]*>([\s\S]*?)</td>', row):
            m = _TICKER_PATTERN.search(td)
            if not m:
                continue
            ticker = m.group(1).strip().upper().replace(".", "-")
            if not ticker or len(ticker) > 6:
                continue
            out.append(ticker)
            break  # one ticker per row

    deduped = sorted(set(out))
    print(f"{label}: parsed {len(deduped)} tickers from Wikipedia", flush=True)
    return deduped


def compute_adv_median(ticker: str, end: date, lookback_days: int) -> float | None:
    """Trailing median daily share volume; None if data unavailable.

    Uses data_cache.fetch (idempotent — returns cached if present, fetches
    via yfinance otherwise) to ensure the parquet exists, then loads it.
    """
    start = end - timedelta(days=lookback_days + 30)
    try:
        data_cache.fetch(ticker, start=start, end=end)
        df = data_cache.load(ticker)
    except Exception as e:
        print(f"  FETCH FAIL {ticker}: {str(e)[:80]}", flush=True)
        return None
    if df is None or df.empty or "Volume" not in df.columns:
        return None
    tail = df["Volume"].tail(lookback_days).dropna()
    if tail.empty:
        return None
    return float(tail.median())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--asof",
        default=date.today().isoformat(),
        help="As-of date for the trailing-volume window. Defaults to today.",
    )
    p.add_argument(
        "--adv-floor",
        type=int,
        default=ADV_FLOOR_SHARES,
        help=f"Minimum trailing-60d median daily volume. Default {ADV_FLOOR_SHARES}.",
    )
    p.add_argument(
        "--max-universe",
        type=int,
        default=None,
        help="If set, cap the candidate list to top-N by trailing volume before "
             "filtering. Useful for fast iteration.",
    )
    p.add_argument("--out", default=str(OUT_PATH))
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the result; do not write the YAML.",
    )
    args = p.parse_args()

    asof = date.fromisoformat(args.asof)
    out_path = Path(args.out)

    sp400 = fetch_sp_constituents("sp400", WIKIPEDIA_SP_PAGES["sp400"])
    sp600 = fetch_sp_constituents("sp600", WIKIPEDIA_SP_PAGES["sp600"])
    sp500 = get_universe("sp500_2026q2")
    candidates = sorted(set(sp400) | set(sp600) | set(sp500))
    print(
        f"Candidate pool: {len(candidates)} unique tickers "
        f"(sp500={len(sp500)}, sp400={len(sp400)}, sp600={len(sp600)})",
        flush=True,
    )
    if args.max_universe and len(candidates) > args.max_universe:
        candidates = candidates[: args.max_universe]
        print(f"Capped to first {args.max_universe} for fast iteration", flush=True)

    survivors: list[tuple[str, float]] = []
    failed: list[str] = []
    below_floor: list[tuple[str, float]] = []
    for i, t in enumerate(candidates, 1):
        adv = compute_adv_median(t, end=asof, lookback_days=ADV_LOOKBACK_DAYS)
        if adv is None:
            failed.append(t)
        elif adv >= args.adv_floor:
            survivors.append((t, adv))
        else:
            below_floor.append((t, adv))
        if i % 50 == 0:
            print(
                f"  ... {i}/{len(candidates)} processed "
                f"(survivors={len(survivors)}, below_floor={len(below_floor)}, "
                f"failed={len(failed)})",
                flush=True,
            )

    survivors.sort(key=lambda x: x[0])
    print(
        f"\nResult: {len(survivors)} survivors, "
        f"{len(below_floor)} below ADV floor, {len(failed)} fetch failures",
        flush=True,
    )

    yaml_text = _render_yaml(
        survivors=survivors,
        asof=asof,
        adv_floor=args.adv_floor,
        lookback_days=ADV_LOOKBACK_DAYS,
        candidate_count=len(candidates),
        below_floor_count=len(below_floor),
        failed_count=len(failed),
    )
    if args.dry_run:
        print("\n--- DRY RUN, would write to", out_path, "---")
        print(yaml_text[:2000])
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml_text, encoding="utf-8")
    print(f"\nWrote {out_path} ({len(survivors)} tickers)")

    # Emit a sidecar JSON with full ADV stats for reproducibility.
    sidecar = out_path.with_suffix(".audit.json")
    sidecar.write_text(
        json.dumps(
            {
                "asof": asof.isoformat(),
                "adv_floor_shares": args.adv_floor,
                "lookback_days": ADV_LOOKBACK_DAYS,
                "survivors": [{"ticker": t, "median_adv_shares": v} for t, v in survivors],
                "below_floor": [{"ticker": t, "median_adv_shares": v} for t, v in below_floor],
                "fetch_failures": failed,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote audit sidecar {sidecar}")


def _render_yaml(
    *,
    survivors: list[tuple[str, float]],
    asof: date,
    adv_floor: int,
    lookback_days: int,
    candidate_count: int,
    below_floor_count: int,
    failed_count: int,
) -> str:
    # Quote tickers to keep PyYAML 1.1-bool-prone strings (ON, NO, Y, TRUE,
    # OFF, YES) typed as str rather than reinterpreted as booleans.
    ticker_lines = "\n".join(f'  - "{t}"' for t, _ in survivors)
    return f"""# liquid_us_2026q2 - cap-agnostic liquid US equity universe, pinned {asof.isoformat()}.
#
# Source: union of (a) S&P 500 constituents (sp500_2026q2), (b) S&P 400
# MidCap constituents (Wikipedia), and (c) S&P 600 SmallCap constituents
# (Wikipedia). Together this is the S&P 1500 - roughly 1500 names spanning
# mega-cap down to small-cap. Filtered to trailing-{lookback_days}-day
# median daily share volume >= {adv_floor:,}.
#
# Build provenance:
#   candidate_pool = {candidate_count} tickers (S&P 1500 union)
#   survivors      = {len(survivors)} (passed ADV floor)
#   below_floor    = {below_floor_count}
#   fetch_failures = {failed_count}
#   built by scripts/build_liquid_us_universe.py on {asof.isoformat()}
#
# Cap segmentation: this universe is intentionally cap-agnostic. The
# framework's liquidity floor (ADV > 500K shares) is the sole gate;
# market cap is recorded but not filtered. Per CLAUDE.md "Required
# Checks" section (revised 2026-05-26): liquidity is the binding
# constraint, not market cap.
#
# Ticker formatting: yfinance convention - share-class separators are
# hyphens, not dots (BRK-B, not BRK.B).
#
# CAVEAT (survivor bias): this is current-extant membership. Tickers
# delisted between the backtest start and the as-of date are NOT in
# this list. The bias is materially worse than sp500_2026q2 because
# small-cap attrition is much higher than large-cap attrition. Per
# tools/deployable_setups.yml methodology disclosures: paper-trade is
# acceptable on this universe; real-capital deployment (gated to
# Q3 2026) MUST be re-validated against a point-in-time membership
# feed.
#
# CAVEAT (slippage): tools/backtest/simulator.py fills limit orders at
# OHLCV levels with no spread/slippage model. This is materially more
# optimistic on small caps than on large caps. Backtest Sharpe on this
# universe will overstate live-realised Sharpe by a wider margin than
# the existing mega/large-cap-only universes.

name: liquid_us_2026q2
pinned_at: {asof.isoformat()}
provenance: |
  S&P 1500 (S&P 500 + S&P 400 MidCap + S&P 600 SmallCap), filtered to
  trailing-{lookback_days}d median daily volume >= {adv_floor:,} shares.
  Built {asof.isoformat()} via scripts/build_liquid_us_universe.py.
notes: |
  Cap-agnostic. Liquidity-only floor (ADV >= {adv_floor:,} shares).
  Survivor-biased; real-capital deployment must re-validate against
  point-in-time membership. Backtest slippage model is missing - live
  Sharpe will be lower than backtest Sharpe by a wider margin than on
  the existing mega/large-cap universes.
tickers:
{ticker_lines}
"""


if __name__ == "__main__":
    main()
