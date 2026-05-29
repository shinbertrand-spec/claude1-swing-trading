"""Market-temperature fetchers (Put-Call, CNN Fear & Greed, AAII, VIX term).

Per spec at ``Bertieboo/Output/2026-05-29-claude1-market-temperature-pass-implementation.md``:
overlay context for the news-research snapshot (schema 1.3). NEVER a gate —
``risk-and-compliance`` and the swing-critic panel use these as informational
input only, per [[ai-arbitrage-compression]].

Four fetchers (all fail-soft — never raise; return ``{"error": "<reason>",
"as_of": None}`` on failure) plus a top-level :func:`fetch_market_temperature`
composer. Per-fetcher TTL is enforced via a thin on-disk JSON cache at
``ledgers/news/_state/market_temperature_cache/<fetcher>.json``.

CLI::

    uv run python -m tools.news_research.market_temperature
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

TOOL = "tools/news_research/market_temperature.py"

DEFAULT_CACHE_DIR = Path("ledgers/news/_state/market_temperature_cache")

# Per-fetcher TTL in seconds. Values from spec § 3.1.
TTL_SECONDS: dict[str, int] = {
    "put_call": 3600,        # 1h
    "fear_greed": 3600,      # 1h
    "aaii": 86400,           # 24h (weekly cadence)
    "vix_term": 900,         # 15m
}

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_HTTP_TIMEOUT = 10  # seconds


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------


def _now_ts() -> float:
    return time.time()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cache_path(fetcher: str, *, cache_dir: Path) -> Path:
    return cache_dir / f"{fetcher}.json"


def cache_get(
    fetcher: str,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    now_fn: Callable[[], float] = _now_ts,
) -> dict | None:
    """Return the cached value if present + fresh; None otherwise."""
    path = _cache_path(fetcher, cache_dir=cache_dir)
    if not path.is_file():
        return None
    try:
        wrapped = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    stored_at = float(wrapped.get("stored_at") or 0)
    ttl = int(wrapped.get("ttl_seconds") or 0)
    if now_fn() - stored_at > ttl:
        return None
    return wrapped.get("value")


def cache_set(
    fetcher: str,
    value: dict,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    now_fn: Callable[[], float] = _now_ts,
) -> None:
    """Write the value to the cache with the fetcher's TTL.

    Never raises — cache failures must not break a successful fetch.
    """
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = _cache_path(fetcher, cache_dir=cache_dir)
        path.write_text(
            json.dumps({
                "stored_at": now_fn(),
                "ttl_seconds": TTL_SECONDS.get(fetcher, 0),
                "value": value,
            }),
            encoding="utf-8",
        )
    except OSError:
        return


# ---------------------------------------------------------------------------
# HTTP helper (urllib — matches project convention from screener.py +
# thematic_portfolio/corpus/press_rss.py; no new deps)
# ---------------------------------------------------------------------------


def _http_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = _HTTP_TIMEOUT,
) -> bytes:
    """GET ``url`` and return the raw body. Raises on non-2xx / network error."""
    base_headers = {"User-Agent": _DEFAULT_UA, "Accept": "*/*"}
    if headers:
        base_headers.update(headers)
    req = urllib.request.Request(url, headers=base_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Error sentinel
# ---------------------------------------------------------------------------


def _err(reason: str) -> dict:
    return {"error": reason, "as_of": None}


# ---------------------------------------------------------------------------
# Fetcher 1 — Fear & Greed (CNN)
# ---------------------------------------------------------------------------


_FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"


def _classify_fear_greed(value: int) -> str:
    if value <= 24:
        return "extreme_fear"
    if value <= 44:
        return "fear"
    if value <= 55:
        return "neutral"
    if value <= 75:
        return "greed"
    return "extreme_greed"


def fetch_fear_greed(
    *,
    http_get: Callable[[str], bytes] = _http_get,
) -> dict:
    """CNN Fear & Greed composite. Daily cadence (cached 1h)."""
    try:
        raw = http_get(_FEAR_GREED_URL)
        doc = json.loads(raw)
        fg = doc.get("fear_and_greed") or {}
        score = fg.get("score")
        if score is None:
            return _err("missing_score_field")
        value = int(round(float(score)))
        ts = fg.get("timestamp")
        # CNN returns ISO; pass through if string, else compose from now.
        as_of = ts if isinstance(ts, str) else _utc_iso()
        return {
            "as_of": as_of,
            "value": value,
            "regime": _classify_fear_greed(value),
            "source": "cnn",
        }
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return _err(f"network: {type(exc).__name__}")
    except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
        return _err(f"parse: {type(exc).__name__}")


# ---------------------------------------------------------------------------
# Fetcher 2 — Put-Call ratio (CBOE)
# ---------------------------------------------------------------------------


_PUT_CALL_URL = (
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIXPCC_History.csv"
)


def fetch_put_call_ratio(
    *,
    http_get: Callable[[str], bytes] = _http_get,
) -> dict:
    """CBOE total + equity-only put/call ratios. Daily cadence (cached 1h).

    The CBOE PCC history CSV exposes the total VIX-relevant put-call ratio.
    Equity-only is not always available from the same endpoint; we surface
    it as ``None`` when the column is absent rather than fail the call.
    """
    try:
        raw = http_get(_PUT_CALL_URL)
        text = raw.decode("utf-8", errors="replace")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) < 2:
            return _err("empty_csv")
        header = [h.strip().lower() for h in lines[0].split(",")]
        last = lines[-1].split(",")
        if len(last) < 2:
            return _err("malformed_row")
        # Date is the first column, total ratio is conventionally the next
        # numeric column.
        date_val = last[0]
        total: float | None = None
        equity: float | None = None
        for i, col in enumerate(header[1:], start=1):
            if i >= len(last):
                break
            try:
                val = float(last[i])
            except ValueError:
                continue
            if total is None and ("total" in col or i == 1):
                total = val
            if "equity" in col:
                equity = val
        if total is None:
            return _err("no_total_column")
        # Normalize date to ISO yyyy-mm-dd if possible
        as_of = date_val
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                as_of = datetime.strptime(date_val, fmt).date().isoformat()
                break
            except ValueError:
                continue
        return {
            "as_of": as_of,
            "total": total,
            "equity": equity,
            "source": "cboe",
        }
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return _err(f"network: {type(exc).__name__}")
    except (ValueError, IndexError) as exc:
        return _err(f"parse: {type(exc).__name__}")


# ---------------------------------------------------------------------------
# Fetcher 3 — AAII weekly survey
# ---------------------------------------------------------------------------


_AAII_HTML_URL = "https://www.aaii.com/sentimentsurvey/sent_results"

# Look for the percent lines in the HTML scrape. The page renders the
# current week's Bullish / Neutral / Bearish percentages in close proximity.
_AAII_PCT_RE = re.compile(
    r"(Bullish|Neutral|Bearish)\s*[^0-9%]{0,40}([0-9]{1,3}(?:\.[0-9]+)?)\s*%",
    re.IGNORECASE,
)

_AAII_DATE_RE = re.compile(
    r"Reported.*?(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE | re.DOTALL
)


def fetch_aaii_survey(
    *,
    http_get: Callable[[str], bytes] = _http_get,
) -> dict:
    """AAII weekly investor sentiment. Weekly cadence (cached 24h).

    HTML-scrape strategy (no new deps). The page renders Bullish / Neutral /
    Bearish as labelled percentages.
    """
    try:
        raw = http_get(_AAII_HTML_URL)
        text = raw.decode("utf-8", errors="replace")
        found: dict[str, float] = {}
        for m in _AAII_PCT_RE.finditer(text):
            label = m.group(1).lower()
            pct = float(m.group(2)) / 100.0
            # First occurrence wins (the page repeats values in tables;
            # the first hit is the headline current week).
            found.setdefault(label, pct)
        bull = found.get("bullish")
        neutral = found.get("neutral")
        bear = found.get("bearish")
        if bull is None or bear is None or neutral is None:
            return _err("missing_percentages")
        if not (0 <= bull <= 1 and 0 <= neutral <= 1 and 0 <= bear <= 1):
            return _err("percentages_out_of_range")
        as_of_week: str | None = None
        m = _AAII_DATE_RE.search(text)
        if m:
            datestr = m.group(1)
            for fmt in ("%m/%d/%Y", "%m/%d/%y"):
                try:
                    as_of_week = datetime.strptime(datestr, fmt).date().isoformat()
                    break
                except ValueError:
                    continue
        return {
            "as_of_week": as_of_week,
            "bull": bull,
            "neutral": neutral,
            "bear": bear,
            "bull_bear_spread": round(bull - bear, 4),
            "source": "aaii",
        }
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return _err(f"network: {type(exc).__name__}")
    except (ValueError, AttributeError) as exc:
        return _err(f"parse: {type(exc).__name__}")


# ---------------------------------------------------------------------------
# Fetcher 4 — VIX term structure (CBOE CSVs)
# ---------------------------------------------------------------------------


_VIX_URL = (
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
)
_VIX9D_URL = (
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv"
)
_VIX3M_URL = (
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv"
)


def _parse_cboe_last_close(text: str) -> tuple[str, float] | None:
    """Return (date_str, close) for the last CBOE history row."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    header = [h.strip().lower() for h in lines[0].split(",")]
    last = lines[-1].split(",")
    if len(last) < 2 or len(last) != len(header):
        return None
    close_idx: int | None = None
    for i, col in enumerate(header):
        if col == "close":
            close_idx = i
            break
    if close_idx is None:
        # Fall back to last column.
        close_idx = len(last) - 1
    try:
        close = float(last[close_idx])
    except ValueError:
        return None
    date_val = last[0]
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            date_val = datetime.strptime(date_val, fmt).date().isoformat()
            break
        except ValueError:
            continue
    return date_val, close


def _classify_vix_term(vix: float, vix9d: float, vix3m: float) -> str:
    if vix3m <= 0 or vix <= 0:
        return "neutral"
    if vix > 0 and vix9d / vix > 1.10:
        return "short_term_stress"
    if vix / vix3m > 1.05:
        return "backwardation"
    if vix / vix3m < 0.95:
        return "contango"
    return "neutral"


def fetch_vix_term_structure(
    *,
    http_get: Callable[[str], bytes] = _http_get,
) -> dict:
    """VIX / VIX9D / VIX3M term-structure ratio. Cached 15m."""
    try:
        vix_raw = http_get(_VIX_URL).decode("utf-8", errors="replace")
        vix9d_raw = http_get(_VIX9D_URL).decode("utf-8", errors="replace")
        vix3m_raw = http_get(_VIX3M_URL).decode("utf-8", errors="replace")
        vix_pair = _parse_cboe_last_close(vix_raw)
        vix9d_pair = _parse_cboe_last_close(vix9d_raw)
        vix3m_pair = _parse_cboe_last_close(vix3m_raw)
        if not (vix_pair and vix9d_pair and vix3m_pair):
            return _err("malformed_csv")
        as_of = vix_pair[0]
        vix, vix9d, vix3m = vix_pair[1], vix9d_pair[1], vix3m_pair[1]
        if vix <= 0:
            return _err("non_positive_vix")
        return {
            "as_of": as_of,
            "vix": vix,
            "vix9d": vix9d,
            "vix3m": vix3m,
            "vix9d_vix": round(vix9d / vix, 4),
            "vix_vix3m": round(vix / vix3m, 4) if vix3m else None,
            "regime": _classify_vix_term(vix, vix9d, vix3m),
            "source": "cboe",
        }
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return _err(f"network: {type(exc).__name__}")
    except (ValueError, IndexError) as exc:
        return _err(f"parse: {type(exc).__name__}")


# ---------------------------------------------------------------------------
# Cache wrappers
# ---------------------------------------------------------------------------


_FETCHERS: dict[str, Callable[..., dict]] = {
    "put_call": fetch_put_call_ratio,
    "fear_greed": fetch_fear_greed,
    "aaii": fetch_aaii_survey,
    "vix_term": fetch_vix_term_structure,
}


def _cached_fetch(
    name: str,
    *,
    http_get: Callable[[str], bytes] = _http_get,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    now_fn: Callable[[], float] = _now_ts,
    use_cache: bool = True,
) -> dict:
    """Cache-wrap one fetcher. Cache hit on fresh, success-only values."""
    if use_cache:
        cached = cache_get(name, cache_dir=cache_dir, now_fn=now_fn)
        if cached is not None:
            return cached
    value = _FETCHERS[name](http_get=http_get)
    if "error" not in value:
        cache_set(name, value, cache_dir=cache_dir, now_fn=now_fn)
    return value


# ---------------------------------------------------------------------------
# Top-level composer
# ---------------------------------------------------------------------------


def fetch_market_temperature(
    *,
    http_get: Callable[[str], bytes] = _http_get,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    now_fn: Callable[[], float] = _now_ts,
    use_cache: bool = True,
) -> dict:
    """Top-level composer. Calls all 4 fetchers, composes the snapshot block.

    Never raises. If all four fetchers fail, returns block with all-error
    children + populated ``errors[]``.
    """
    children: dict[str, dict] = {}
    errors: list[str] = []
    for name in ("put_call", "fear_greed", "aaii", "vix_term"):
        try:
            child = _cached_fetch(
                name,
                http_get=http_get,
                cache_dir=cache_dir,
                now_fn=now_fn,
                use_cache=use_cache,
            )
        except Exception as exc:  # defense-in-depth — fetchers already fail-soft
            child = _err(f"unexpected: {type(exc).__name__}: {exc}")
        children[name] = child
        if "error" in child:
            errors.append(f"{name}: {child['error']}")

    # Compose top-level as_of as the max of children's as_of (or as_of_week
    # for AAII), falling back to now when no child succeeded.
    candidates: list[str] = []
    for name, child in children.items():
        if "error" in child:
            continue
        v = child.get("as_of") or child.get("as_of_week")
        if isinstance(v, str):
            candidates.append(v)
    as_of = max(candidates) if candidates else _utc_iso()
    return {
        "as_of": as_of,
        "put_call": children["put_call"],
        "fear_greed": children["fear_greed"],
        "aaii": children["aaii"],
        "vix_term": children["vix_term"],
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Latest-snapshot loader (consumer-side helper)
# ---------------------------------------------------------------------------


DEFAULT_NEWS_ROOT = Path("ledgers/news")
STALE_SNAPSHOT_SECONDS = 2 * 3600  # 2h per spec § 3.4


def load_latest_market_temperature(
    *,
    news_root: Path = DEFAULT_NEWS_ROOT,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict | None:
    """Return the freshest ``market_temperature`` block from disk, or None.

    Stale-snapshot detection per spec § 3.4: if the latest snapshot is more
    than 2 hours older than ``now_fn()`` OR all four fetchers in the block
    are in error state, return ``None`` so consumers receive a null block
    and notice the gap.
    """
    if not news_root.exists():
        return None
    try:
        import yaml
    except ImportError:
        return None
    day_dirs = sorted(
        (p for p in news_root.iterdir()
         if p.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", p.name)),
        reverse=True,
    )
    for day_dir in day_dirs:
        hh_files = sorted(
            (p for p in day_dir.iterdir()
             if p.is_file() and re.match(r"^\d{2}\.yml$", p.name)),
            reverse=True,
        )
        for hh_file in hh_files:
            try:
                doc = yaml.safe_load(hh_file.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            block = doc.get("market_temperature")
            if not isinstance(block, dict):
                continue
            # All-errors → stale
            errors = block.get("errors") or []
            children = ("put_call", "fear_greed", "aaii", "vix_term")
            if len(errors) >= len(children):
                return None
            # Snapshot-age check via meta.asof / meta.fetched_at
            meta = doc.get("meta") or {}
            asof_str = meta.get("fetched_at") or meta.get("asof") or meta.get("snapshot_id")
            if isinstance(asof_str, str):
                try:
                    cleaned = asof_str.replace("Z", "+00:00")
                    asof = datetime.fromisoformat(cleaned)
                    if asof.tzinfo is None:
                        asof = asof.replace(tzinfo=timezone.utc)
                    if (now_fn() - asof).total_seconds() > STALE_SNAPSHOT_SECONDS:
                        return None
                except ValueError:
                    pass
            return block
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(
        prog="tools.news_research.market_temperature",
        description="Fetch the composed market-temperature block (4 fetchers).",
    )
    p.add_argument("--no-cache", action="store_true", help="Bypass the on-disk cache.")
    args = p.parse_args()
    block = fetch_market_temperature(use_cache=not args.no_cache)
    json.dump(block, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
