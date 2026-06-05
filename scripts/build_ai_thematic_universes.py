"""Build the ``ai_thematic_pure_2026q2`` and ``ai_thematic_broad_2026q2``
registered universes.

Hand-curated thematic ticker lists (AI primes + run-offs/gushers) sourced
from the morning-research process on 2026-05-29, run through the framework's
ADV > 500K floor before being pinned. Mirrors the build/audit pattern of
``scripts/build_liquid_us_universe.py`` (ADV-filtering + sidecar JSON for
provenance + idempotent re-runs).

Pipeline:

1. Take the embedded hand-curated lists (PURE_TICKERS + BROAD_TICKERS).
2. For each, fetch a year of OHLCV via ``tools.backtest.data_cache``
   (re-uses cache; only network-fetches new tickers).
3. Compute trailing-60d median daily share volume.
4. Filter to median >= 500K shares.
5. Write each as ``tools/quant_strategies/_universes/<name>.yml`` plus a
   ``.audit.json`` sidecar (per-ticker ADV + provenance + drop reasons).

Usage::

    uv run python scripts/build_ai_thematic_universes.py
    uv run python scripts/build_ai_thematic_universes.py --dry-run

Re-run if the curated lists change. Bucket assignments are preserved as
audit-trail comments in the YAML body.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.backtest import data_cache


ADV_FLOOR_SHARES = 500_000
ADV_LOOKBACK_DAYS = 60

OUT_DIR = (
    Path(__file__).resolve().parent.parent
    / "tools/quant_strategies/_universes"
)


# ---------------------------------------------------------------------------
# Hand-curated bucket lists. Each entry is (bucket_label, [tickers]).
# Edit here to add/remove names. Bucket labels are preserved as YAML comments
# for audit trail.
# ---------------------------------------------------------------------------

PURE_BUCKETS: list[tuple[str, list[str]]] = [
    ("AI primes / compute leaders", ["NVDA", "AMD", "AVGO", "MRVL", "ASML", "QCOM"]),
    ("Hyperscalers / inference platforms", ["MSFT", "GOOGL", "META", "AMZN", "ORCL"]),
    ("Power generation (nuclear + IPP)", ["CEG", "VST", "TLN", "NRG", "BWXT"]),
    ("Cooling / thermal management", ["VRT", "ETN", "MOD", "SMCI"]),
    ("Data-center REITs", ["EQIX", "DLR", "IRM"]),
    ("Networking / optical / interconnect", ["ANET", "CIEN", "COHR", "LITE", "CRDO"]),
    ("Memory / HBM", ["MU", "WDC", "STX"]),
    ("Semicap", ["AMAT", "LRCX"]),
    ("Software inference / accelerated AI infra", ["PLTR", "SNOW", "NOW", "DDOG"]),
    ("AI-native compute / cloud", ["NBIS", "CRWV"]),
    ("Smaller pure-play infra", ["ALAB", "AEHR"]),
]

# Broad = pure superset + the additional runoff/gushers buckets below.
BROAD_EXTRA_BUCKETS: list[tuple[str, list[str]]] = [
    ("Power & utilities runoff", ["AEP", "D", "DUK", "EXC", "NEE", "SO", "XEL",
                                   "PEG", "PCG", "EIX", "SRE", "ETR", "WEC",
                                   "AES", "PPL"]),
    ("Nuclear fuel / SMR", ["CCJ", "UEC", "LEU", "OKLO", "SMR"]),
    ("Grid / transmission / electrification",
     ["GEV", "EME", "MTZ", "PWR", "FLR", "J", "PRIM", "ROK", "EMR", "HUBB"]),
    ("Industrial gases / cooling fluids", ["APD", "LIN", "CE", "DD"]),
    ("Copper / electrification metals", ["FCX", "SCCO", "TECK", "ALB", "MP"]),
    ("Construction / DC build-out", ["STRL", "GVA", "EXP", "MLM", "VMC", "MAS"]),
    ("Networking adjacencies", ["PRGS", "BMI", "BDC", "TEL", "GLW"]),
    ("Semiconductor adjacencies",
     ["TSM", "KLAC", "ON", "MCHP", "NXPI", "ADI", "MPWR", "ENTG", "TER", "FORM"]),
    ("Memory / storage adjacencies", ["NTAP", "PSTG"]),
    ("AI-enabled software",
     ["ADBE", "CRM", "INTU", "WDAY", "ANSS", "CDNS", "SNPS", "ADSK", "MDB", "FTNT"]),
    ("Data infrastructure / observability",
     ["ESTC", "GTLB", "NET", "CFLT", "OKTA", "S"]),
    ("Application AI / consumer", ["AAPL", "TSLA", "SPOT", "DUOL"]),
    ("Robotics / automation", ["ABB", "HON"]),
    ("EDA / IP", ["ARM"]),
    ("Specialty / second-derivative",
     ["IOT", "BBAI", "AI", "SOUN", "RKLB", "ACHR", "AVAV", "KTOS",
      "LMT", "RTX", "GE", "CAT", "TT", "JCI"]),
]


def _flatten(buckets: list[tuple[str, list[str]]]) -> list[str]:
    out: list[str] = []
    for _label, tickers in buckets:
        out.extend(tickers)
    return out


def _dedupe_preserve_buckets(
    buckets: list[tuple[str, list[str]]]
) -> list[tuple[str, list[str]]]:
    """Drop within-bucket dups and dups already seen in earlier buckets."""
    seen: set[str] = set()
    cleaned: list[tuple[str, list[str]]] = []
    for label, tickers in buckets:
        kept: list[str] = []
        for t in tickers:
            tu = t.upper()
            if tu in seen:
                continue
            seen.add(tu)
            kept.append(tu)
        cleaned.append((label, kept))
    return cleaned


def compute_adv_median(ticker: str, end: date, lookback_days: int) -> float | None:
    """Trailing median daily share volume; None if data unavailable."""
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


def _filter_buckets(
    buckets: list[tuple[str, list[str]]],
    asof: date,
    adv_floor: int,
) -> tuple[list[tuple[str, list[str]]], dict[str, float], list[tuple[str, float]],
            list[str]]:
    """Filter every bucket's tickers by ADV floor.

    Returns:
      kept_buckets: buckets with sub-floor + failed tickers stripped
      adv_map: ticker -> median ADV (survivors only)
      below_floor: [(ticker, adv), ...]
      failed: [ticker, ...] fetch failures
    """
    adv_map: dict[str, float] = {}
    below_floor: list[tuple[str, float]] = []
    failed: list[str] = []
    kept_buckets: list[tuple[str, list[str]]] = []
    seen_tickers: list[str] = []
    for _label, tickers in buckets:
        seen_tickers.extend(tickers)
    total = len(seen_tickers)
    progress = 0
    for label, tickers in buckets:
        kept: list[str] = []
        for t in tickers:
            progress += 1
            adv = compute_adv_median(t, end=asof, lookback_days=ADV_LOOKBACK_DAYS)
            if adv is None:
                failed.append(t)
            elif adv >= adv_floor:
                adv_map[t] = adv
                kept.append(t)
            else:
                below_floor.append((t, adv))
            if progress % 20 == 0:
                print(f"  ... {progress}/{total} processed "
                      f"(kept={len(adv_map)}, below_floor={len(below_floor)}, "
                      f"failed={len(failed)})", flush=True)
        kept_buckets.append((label, kept))
    return kept_buckets, adv_map, below_floor, failed


def _render_universe_yaml(
    *,
    name: str,
    short_descriptor: str,
    long_descriptor: str,
    kept_buckets: list[tuple[str, list[str]]],
    survivors_sorted: list[str],
    asof: date,
    adv_floor: int,
    lookback_days: int,
    candidate_count: int,
    below_floor_count: int,
    failed_count: int,
) -> str:
    """Render YAML body with bucket-annotated comment block + flat alphabetical
    tickers list (matching the schema other registered universes use)."""
    bucket_lines: list[str] = ["# Bucket assignments (audit trail; ticker list is flat alphabetical below):"]
    for label, tickers in kept_buckets:
        if not tickers:
            continue
        bucket_lines.append(f"#   - {label} ({len(tickers)}): {', '.join(tickers)}")
    bucket_comment = "\n".join(bucket_lines)

    ticker_lines = "\n".join(f'  - "{t}"' for t in survivors_sorted)
    return f"""# {name} - {short_descriptor}, pinned {asof.isoformat()}.
#
# {long_descriptor}
#
# Build provenance:
#   candidate_pool = {candidate_count} tickers (hand-curated)
#   survivors      = {len(survivors_sorted)} (passed ADV floor)
#   below_floor    = {below_floor_count}
#   fetch_failures = {failed_count}
#   built by scripts/build_ai_thematic_universes.py on {asof.isoformat()}
#
# Sourcing caveat: hand-curated from the swing morning-research process on
# 2026-05-29. The three vault Tier 3 wikilinks ([[ai-capex-announcements]],
# [[power-sector]], [[semi-inventory]]) are sources:0 placeholders, not
# sourced taxonomies. Hand-curation is the only available source-of-truth
# this iteration. Backlog: source those concept pages before iteration 2.
#
# Cap segmentation: this universe is intentionally cap-agnostic. The
# framework's liquidity floor (ADV >= {adv_floor:,} shares) is the sole
# gate; market cap is recorded but not filtered.
#
# Ticker formatting: yfinance convention - share-class separators are
# hyphens, not dots (BRK-B, not BRK.B).
#
# CAVEAT (survivor bias): current-extant membership. Tickers delisted
# between the backtest start and the as-of date are NOT in this list.
# Paper-trade is acceptable; real-capital deployment (gated to Q3 2026)
# MUST be re-validated against a point-in-time membership feed.
#
# CAVEAT (slippage): tools/backtest/simulator.py fills limit orders at
# OHLCV levels with no spread/slippage model. Backtest Sharpe on this
# universe will overstate live-realised Sharpe.

{bucket_comment}

name: {name}
pinned_at: {asof.isoformat()}
provenance: |
  Hand-curated AI-thematic ticker list (2026-05-29 morning research),
  filtered to trailing-{lookback_days}d median daily volume >= {adv_floor:,}
  shares. Built {asof.isoformat()} via scripts/build_ai_thematic_universes.py.
notes: |
  {long_descriptor}
tickers:
{ticker_lines}
"""


def _write_audit_sidecar(
    *,
    out_path: Path,
    name: str,
    asof: date,
    adv_floor: int,
    kept_buckets: list[tuple[str, list[str]]],
    adv_map: dict[str, float],
    below_floor: list[tuple[str, float]],
    failed: list[str],
) -> None:
    sidecar = out_path.with_suffix(".audit.json")
    sidecar.write_text(
        json.dumps(
            {
                "universe": name,
                "asof": asof.isoformat(),
                "adv_floor_shares": adv_floor,
                "lookback_days": ADV_LOOKBACK_DAYS,
                "buckets": [
                    {
                        "label": label,
                        "tickers": [
                            {"ticker": t, "median_adv_shares": adv_map.get(t)}
                            for t in tickers
                        ],
                    }
                    for label, tickers in kept_buckets
                ],
                "survivors": [
                    {"ticker": t, "median_adv_shares": v}
                    for t, v in sorted(adv_map.items())
                ],
                "below_floor": [
                    {"ticker": t, "median_adv_shares": v}
                    for t, v in below_floor
                ],
                "fetch_failures": failed,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote audit sidecar {sidecar}")


def build_one(
    *,
    name: str,
    short_descriptor: str,
    long_descriptor: str,
    buckets: list[tuple[str, list[str]]],
    asof: date,
    adv_floor: int,
    dry_run: bool,
) -> None:
    print(f"\n=== {name} ===")
    cleaned = _dedupe_preserve_buckets(buckets)
    candidates = _flatten(cleaned)
    print(f"Candidate pool: {len(candidates)} unique tickers", flush=True)
    kept_buckets, adv_map, below_floor, failed = _filter_buckets(
        cleaned, asof=asof, adv_floor=adv_floor
    )
    survivors_sorted = sorted(adv_map.keys())
    print(f"\nResult: {len(survivors_sorted)} survivors, "
          f"{len(below_floor)} below ADV floor, {len(failed)} fetch failures",
          flush=True)

    yaml_text = _render_universe_yaml(
        name=name,
        short_descriptor=short_descriptor,
        long_descriptor=long_descriptor,
        kept_buckets=kept_buckets,
        survivors_sorted=survivors_sorted,
        asof=asof,
        adv_floor=adv_floor,
        lookback_days=ADV_LOOKBACK_DAYS,
        candidate_count=len(candidates),
        below_floor_count=len(below_floor),
        failed_count=len(failed),
    )
    out_path = OUT_DIR / f"{name}.yml"
    if dry_run:
        print(f"\n--- DRY RUN, would write to {out_path} ---")
        print(yaml_text[:1500])
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml_text, encoding="utf-8")
    print(f"\nWrote {out_path} ({len(survivors_sorted)} tickers)")
    _write_audit_sidecar(
        out_path=out_path,
        name=name,
        asof=asof,
        adv_floor=adv_floor,
        kept_buckets=kept_buckets,
        adv_map=adv_map,
        below_floor=below_floor,
        failed=failed,
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--asof", default=date.today().isoformat())
    p.add_argument("--adv-floor", type=int, default=ADV_FLOOR_SHARES)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--only",
        choices=["pure", "broad", "both"],
        default="both",
        help="Build only one of the two universes (useful for fast iteration).",
    )
    args = p.parse_args()
    asof = date.fromisoformat(args.asof)

    if args.only in ("pure", "both"):
        build_one(
            name="ai_thematic_pure_2026q2",
            short_descriptor="AI primes pure-play universe (~41 tickers)",
            long_descriptor=(
                "Tight pure-play AI universe: AI primes + compute leaders + "
                "hyperscalers + power gen + cooling + DC REITs + networking + "
                "memory + semicap + AI software/infra. Liquidity-only floor "
                "(ADV >= 500K)."
            ),
            buckets=PURE_BUCKETS,
            asof=asof,
            adv_floor=args.adv_floor,
            dry_run=args.dry_run,
        )

    if args.only in ("broad", "both"):
        broad_buckets = PURE_BUCKETS + BROAD_EXTRA_BUCKETS
        build_one(
            name="ai_thematic_broad_2026q2",
            short_descriptor="AI runoff/gushers broad universe (~140 tickers)",
            long_descriptor=(
                "Broad AI-runoff/gushers universe: superset of "
                "ai_thematic_pure_2026q2 + power & utilities + nuclear fuel/SMR "
                "+ grid/transmission + industrial gases + copper metals + "
                "construction/DC + semi/networking/software/data-infra "
                "adjacencies + application AI + robotics + EDA + specialty. "
                "Liquidity-only floor (ADV >= 500K)."
            ),
            buckets=broad_buckets,
            asof=asof,
            adv_floor=args.adv_floor,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
