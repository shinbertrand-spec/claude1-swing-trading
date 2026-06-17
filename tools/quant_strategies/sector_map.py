"""Per-ticker sector classification (SPDR sector-ETF tag) for the universe.

Sector-neutralization needs a sector per name. Default source is the SEC SIC
code (via the submissions API — reliable + throttled + cached, unlike yfinance
``.info`` which is slow and flaky at universe scale); yfinance GICS is kept as
an alternate fetcher. Sector membership is ~time-invariant over a backtest
window, so a single latest snapshot per ticker (disk-cached) is an accepted
fidelity trade-off — flagged here rather than silently assumed point-in-time.

CLI (prewarm the cache for a universe)::

    uv run python -m tools.quant_strategies.sector_map --universe liquid_us_2026q2
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# yfinance GICS sector string → SPDR sector ETF (mirror of the screener's map).
YF_SECTOR_TO_ETF: dict[str, str] = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Industrials": "XLI",
    "Communication Services": "XLC",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
}

UNKNOWN = "UNKNOWN"

_BASE = Path(__file__).resolve().parents[2]
CACHE_PATH_DEFAULT = _BASE / "tools" / "cache" / "sector_map.json"
CACHE_TTL_SECONDS = 30 * 86400  # sectors rarely change; refresh monthly

SectorFetcher = Callable[[str], Optional[str]]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sic_to_etf(sic: int) -> str:
    """Map a 4-digit SEC SIC code to a SPDR sector ETF.

    4-digit carve-outs first (drugs / semis / software / REITs / etc.), then the
    2-digit SIC major group. Coarse by construction — SIC predates GICS — but
    good enough to remove the dominant sector tilt from a factor z-score.
    """
    s = int(sic)
    g = s // 100  # 2-digit major group
    specific = {
        # drugs / medicinal chemicals / biological products
        2833: "XLV", 2834: "XLV", 2835: "XLV", 2836: "XLV",
        3674: "XLK",                       # semiconductors
        5411: "XLP",                       # grocery stores
        6798: "XLRE",                      # REITs
    }
    if s in specific:
        return specific[s]
    if 3570 <= s <= 3579 or 3670 <= s <= 3679:
        return "XLK"                       # computers + electronic components
    if 3840 <= s <= 3849:
        return "XLV"                       # medical instruments
    if 3710 <= s <= 3716:
        return "XLY"                       # motor vehicles
    if 3720 <= s <= 3728 or 3760 <= s <= 3769:
        return "XLI"                       # aircraft / guided missiles
    if 7370 <= s <= 7379:
        return "XLK"                       # software + IT services
    group = {
        1: "XLB", 7: "XLB", 8: "XLB", 9: "XLB",        # ag/forestry/fishing
        10: "XLB", 11: "XLB", 12: "XLB", 14: "XLB",    # mining (non-oil)
        13: "XLE", 29: "XLE",                          # oil & gas, refining
        15: "XLI", 16: "XLI", 17: "XLI",               # construction
        20: "XLP", 21: "XLP",                          # food, tobacco
        22: "XLY", 23: "XLY", 25: "XLY", 31: "XLY", 39: "XLY",  # textile/apparel/furniture/leather/misc-mfg
        24: "XLB", 26: "XLB", 28: "XLB", 30: "XLB", 32: "XLB", 33: "XLB",  # wood/paper/chem/rubber/stone/metal
        27: "XLC",                                      # printing/publishing
        34: "XLI", 35: "XLI",                          # fabricated metal + machinery (non-computer)
        36: "XLK", 38: "XLK",                          # electronics + instruments
        37: "XLI",                                      # transportation equipment (non motor-vehicle)
        40: "XLI", 41: "XLI", 42: "XLI", 44: "XLI", 45: "XLI", 46: "XLI", 47: "XLI",  # transportation
        48: "XLC",                                      # communications
        49: "XLU",                                      # utilities
        50: "XLI", 51: "XLP",                          # wholesale durable / nondurable
        52: "XLY", 53: "XLY", 55: "XLY", 56: "XLY", 57: "XLY", 59: "XLY",  # retail
        54: "XLP", 58: "XLY",                          # food stores / eating places
        60: "XLF", 61: "XLF", 62: "XLF", 63: "XLF", 64: "XLF", 67: "XLF",  # finance/insurance
        65: "XLRE",                                     # real estate
        70: "XLY", 72: "XLY", 75: "XLY", 78: "XLC", 79: "XLC",  # hotels/services/auto/movies/recreation
        73: "XLI",                                      # business services (non-software 737x handled above)
        80: "XLV", 87: "XLI",                          # health services / eng+research
        82: "XLY", 83: "XLY", 86: "XLY",               # education / social
    }
    return group.get(g, UNKNOWN)


def _sec_sic_fetcher_factory() -> SectorFetcher:
    """Default fetcher: SEC submissions API → SIC → SPDR ETF (throttled+cached)."""
    from ..fundamentals import pit_fundamentals as pf
    cik_map = pf.load_ticker_cik_map()

    def fetch(ticker: str) -> str:
        cik = cik_map.get(ticker.upper().strip())
        if not cik:
            return UNKNOWN
        try:
            data = pf._http_get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
            sic = data.get("sic")
            return sic_to_etf(int(sic)) if sic else UNKNOWN
        except Exception:        # noqa: BLE001
            return UNKNOWN
    return fetch


def _yf_sector_fetcher(ticker: str) -> Optional[str]:
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        sec = info.get("sector")
        return YF_SECTOR_TO_ETF.get(sec, UNKNOWN) if sec else UNKNOWN
    except Exception:        # noqa: BLE001 — network/parse failures → unknown
        return UNKNOWN


def build_sector_map(
    tickers: list[str],
    *,
    cache_path: Optional[Path] = None,
    fetcher: Optional[SectorFetcher] = None,
    use_cache: bool = True,
) -> dict[str, str]:
    """{ticker -> SPDR sector ETF}. Disk-cached; only fetches missing names."""
    cache_path = cache_path or CACHE_PATH_DEFAULT
    cached: dict[str, str] = {}
    if use_cache and cache_path.is_file():
        try:
            data = json.loads(cache_path.read_text())
            if (time.time() - data.get("_cached_at_epoch", 0)) <= CACHE_TTL_SECONDS:
                cached = data.get("map", {})
        except (OSError, json.JSONDecodeError):
            cached = {}

    out = {t: cached[t] for t in tickers if t in cached}
    missing = [t for t in tickers if t not in out]
    if missing and fetcher is None:        # build the SEC fetcher lazily (1 CIK-map load)
        fetcher = _sec_sic_fetcher_factory()
    for t in missing:
        out[t] = fetcher(t) or UNKNOWN

    if use_cache and missing:
        merged = {**cached, **out}
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(
            {"_cached_at_epoch": time.time(), "_cached_at_iso": _utc_now_iso(), "map": merged}))
    return out


def main() -> None:
    from ._universe import resolve_universe_tickers
    p = argparse.ArgumentParser(prog="tools.quant_strategies.sector_map")
    p.add_argument("--universe", required=True)
    args = p.parse_args()
    tickers = resolve_universe_tickers({"universe": {"name": args.universe}})
    m = build_sector_map(tickers)
    n_known = sum(1 for v in m.values() if v != UNKNOWN)
    print(f"sector_map: {n_known}/{len(m)} classified ({len(m) - n_known} unknown)")


if __name__ == "__main__":
    main()
