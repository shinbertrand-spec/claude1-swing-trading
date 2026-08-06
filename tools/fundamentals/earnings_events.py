"""Earnings-events pipeline — EDGAR-only ingest for ``event_earnings_drift``.

Builds the events file the :mod:`tools.quant_strategies._kinds.event_earnings_drift`
KIND consumes, per the FROZEN filter committed 2026-08-06 in
``ledgers/improvements/2026-08-06-earnings-drift-frozen-filter.md`` (mirrored in the
authority spec ``plans/2026-07-25-event-earnings-drift-candidate-spec.md``):

    An earnings event is an EDGAR filing of form 8-K / 8-K/A whose ``items`` contain
    ``2.02``, with a parseable ``acceptanceDateTime``. Timing (US/Eastern from the UTC
    acceptance): BMO < 09:30, AMC >= 16:00, else unknown (treated as AMC). Multiple
    qualifying filings for one ticker within 3 calendar days collapse to the earliest.
    NO 8.01/9.01 widening — the filter is frozen for the trial's life.

Point-in-time ticker→CIK (build-handoff §2.1, option 1 — frozen sidecar):
resolving a historical universe through TODAY's ``company_tickers.json`` fails
silently on successor entities (verified: XOM → 2026 holdco CIK 2115436 which filed
nothing in 2025 Q2; the in-window filer is CIK 34088). ``build_cik_map`` resolves each
constituent to the CIK that actually filed during the window:

    1. today's map (ticker normalized ``.`` → ``-``), then VALIDATE: the candidate
       CIK's earliest filing date must be <= window end (an entity born after the
       window cannot be the historical filer);
    2. failures + unmapped → predecessor chase via the EDGAR full-text-search entity
       autocomplete, each candidate validated the same way PLUS the ticker must appear
       in the candidate's own ``tickers`` list;
    3. still unresolved → ``cik: null``, carried in the sidecar and disclosed —
       excluded from the trial, never silently dropped.

Frozen sidecar: ``tools/quant_strategies/_universes/liquid_us_2026q2_cik.yml``
(immutable-by-convention, like every universe file). Acceptance (handoff §2.1): the
probe's 2025-Q2 coverage count re-run with this map must EXCEED the probe's 96.5%
floor — ``coverage`` subcommand.

Session alignment (spec §3b, Berkman-Truong — NON-NEGOTIABLE):
    BMO on trading day T  → reaction session T;   ear = close(T-1) → close(T), excess SPY.
    AMC on trading day T  → reaction session T+1; ear = close(T) → close(T+1), excess SPY.
    unknown → AMC treatment. Entry (in the KIND) = first bar STRICTLY AFTER reaction_date.

``ear_rank_pct`` (frozen definition): percentile of the event's ear among all universe
events with STRICTLY EARLIER reaction_date within the trailing 92 calendar days;
requires >= 30 comparators, else null (event unusable for the top-tail filter — warm-up
handles). ``prior_pos_ear_count``: count of positive ear among the ticker's last <= 4
prior observed events (a tag-gap quarter widens the window's calendar span; it does not
bias it).

CLI:
    uv run python -m tools.fundamentals.earnings_events cikmap    # build + freeze sidecar
    uv run python -m tools.fundamentals.earnings_events coverage  # §2.1 acceptance count
    uv run python -m tools.fundamentals.earnings_events events    # build events YAML
"""
from __future__ import annotations

import json
import os
import time
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from zoneinfo import ZoneInfo

import yaml

from .edgar_eps import EDGAR_IDENTITY_ENV, DEFAULT_IDENTITY

_ROOT = Path(__file__).resolve().parent.parent.parent
UNIVERSE_NAME = "liquid_us_2026q2"
SIDECAR_PATH = _ROOT / "tools" / "quant_strategies" / "_universes" / f"{UNIVERSE_NAME}_cik.yml"
EVENTS_PATH = _ROOT / "ledgers" / "earnings" / "events" / f"{UNIVERSE_NAME}.yml"
CACHE_DIR = _ROOT / "ledgers" / "earnings" / "_cache"

WINDOW_START = date(2016, 1, 1)          # 2016 warm-up for history conditioning
WINDOW_END = date(2025, 12, 31)
ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
SLEEP_S = 0.12                            # ~8 req/s, under SEC's 10/s guidance
COLLAPSE_DAYS = 3                         # amendment/duplicate suppression (frozen)
RANK_TRAIL_DAYS = 92                      # trailing cross-sectional window (frozen)
RANK_MIN_COMPARATORS = 30                 # frozen
HISTORY_WINDOW_EVENTS = 4                 # last-4-observed-events (frozen)


# ---------------------------------------------------------------------------
# EDGAR HTTP layer (disk-cached, rate-limited)
# ---------------------------------------------------------------------------


def _identity() -> str:
    return os.environ.get(EDGAR_IDENTITY_ENV) or DEFAULT_IDENTITY


def _get_json(url: str, cache_key: str, *, session=None) -> Optional[dict]:
    """GET a JSON URL with disk cache + retries. Returns None on 404/failure."""
    import requests

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / cache_key
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    sess = session or requests
    for attempt in range(3):
        try:
            r = sess.get(url, headers={"User-Agent": _identity()}, timeout=30)
            if r.status_code == 200:
                j = r.json()
                p.write_text(json.dumps(j), encoding="utf-8")
                time.sleep(SLEEP_S)
                return j
            if r.status_code == 404:
                time.sleep(SLEEP_S)
                return None
            time.sleep(1.5 * (attempt + 1))
        except Exception:  # noqa: BLE001 — network layer; retry then give up
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_submissions(cik: int) -> Optional[dict]:
    cik10 = f"{cik:010d}"
    return _get_json(f"https://data.sec.gov/submissions/CIK{cik10}.json", f"CIK{cik10}.json")


def iter_filings(sub: dict, *, lo: date = WINDOW_START, hi: date = WINDOW_END) -> Iterator[tuple[str, str, str, str]]:
    """Yield ``(form, items, acceptanceDateTime, filingDate)`` for recent + any older
    pages overlapping [lo, hi]. A truncated ``recent`` window must not fake a miss."""
    recent = sub.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    yield from zip(forms, recent.get("items", [""] * len(forms)),
                   recent.get("acceptanceDateTime", [""] * len(forms)),
                   recent.get("filingDate", [""] * len(forms)))
    dates = recent.get("filingDate", [])
    reaches_back = bool(dates) and min(dates) <= lo.isoformat()
    if not reaches_back:
        for f in sub.get("filings", {}).get("files", []):
            if f.get("filingFrom", "9999") > hi.isoformat():
                continue
            if f.get("filingTo", "0000") < lo.isoformat():
                continue
            j = _get_json(f"https://data.sec.gov/submissions/{f['name']}", f["name"])
            if not j:
                continue
            jforms = j.get("form", [])
            yield from zip(jforms, j.get("items", [""] * len(jforms)),
                           j.get("acceptanceDateTime", [""] * len(jforms)),
                           j.get("filingDate", [""] * len(jforms)))


def _earliest_filing_date(sub: dict) -> Optional[str]:
    """Earliest filingDate across recent + the paginated index (no page fetches —
    the ``files`` entries carry their own filingFrom bounds)."""
    dates = list(sub.get("filings", {}).get("recent", {}).get("filingDate", []))
    froms = [f.get("filingFrom") for f in sub.get("filings", {}).get("files", []) if f.get("filingFrom")]
    cands = [d for d in dates + froms if d]
    return min(cands) if cands else None


# ---------------------------------------------------------------------------
# Frozen filter + timing classification
# ---------------------------------------------------------------------------


def acceptance_to_et(acc: str) -> Optional[datetime]:
    """SEC submissions ``acceptanceDateTime`` (UTC, ``Z``-suffixed) → ET datetime."""
    if not acc:
        return None
    try:
        dt = datetime.fromisoformat(acc.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(ET)
    except (ValueError, TypeError):
        return None


def classify_timing(dt_et: datetime) -> str:
    """FROZEN: BMO < 09:30 ET, AMC >= 16:00 ET, else unknown (treated as AMC)."""
    t = dt_et.time()
    if t < datetime(2000, 1, 1, 9, 30).time():
        return "bmo"
    if t >= datetime(2000, 1, 1, 16, 0).time():
        return "amc"
    return "unknown"


def is_earnings_filing(form: str, items: str) -> bool:
    """FROZEN filter: 8-K / 8-K/A with item 2.02. No 8.01/9.01 widening."""
    return form in ("8-K", "8-K/A") and "2.02" in (items or "")


def extract_raw_events(sub: dict, ticker: str) -> list[dict]:
    """Per-ticker raw events (frozen filter + 3-day collapse), window-bounded."""
    rows = []
    for form, items, acc, _fd in iter_filings(sub):
        if not is_earnings_filing(form, items):
            continue
        dt_et = acceptance_to_et(acc)
        if dt_et is None:
            continue
        d = dt_et.date()
        if not (WINDOW_START <= d <= WINDOW_END):
            continue
        rows.append({"ticker": ticker, "announce_date": d, "timing": classify_timing(dt_et)})
    rows.sort(key=lambda r: r["announce_date"])
    collapsed: list[dict] = []
    for r in rows:
        if collapsed and (r["announce_date"] - collapsed[-1]["announce_date"]).days <= COLLAPSE_DAYS:
            continue                      # amendment/duplicate: keep the earliest
        collapsed.append(r)
    return collapsed


# ---------------------------------------------------------------------------
# PIT ticker→CIK map (handoff §2.1, option 1)
# ---------------------------------------------------------------------------


def _today_ticker_map() -> dict[str, int]:
    j = _get_json("https://www.sec.gov/files/company_tickers.json", "company_tickers.json")
    out: dict[str, int] = {}
    for _, row in (j or {}).items():
        out[str(row["ticker"]).upper()] = int(row["cik_str"])
    return out


def _valid_for_window(sub: Optional[dict]) -> bool:
    """A CIK can be the historical filer only if it existed during the window."""
    if not sub:
        return False
    earliest = _earliest_filing_date(sub)
    return earliest is not None and earliest <= WINDOW_END.isoformat()


def _browse_edgar_cik(ticker: str) -> Optional[int]:
    """Resolve a ticker via EDGAR's classic browse endpoint, whose own ticker
    table retains many DELISTED mappings today's ``company_tickers.json`` drops
    (verified: AEP->4904, XOM->34088, VSCO, IAC, GTLS). Deterministic — no
    name fuzz, no wrong-company adoption risk."""
    import re
    import requests

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = CACHE_DIR / f"browse_{ticker.upper().replace('.', '-')}.txt"
    if key.exists():
        txt = key.read_text(encoding="utf-8")
        return int(txt) if txt.strip().isdigit() else None
    url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
           f"&CIK={ticker.upper()}&type=8-K&dateb=&owner=include&count=1&output=atom")
    try:
        r = requests.get(url, headers={"User-Agent": _identity()}, timeout=30)
        time.sleep(SLEEP_S)
        m = re.search(r"CIK=(\d{10})", r.text) if r.status_code == 200 else None
        cik = int(m.group(1)) if m else None
    except Exception:  # noqa: BLE001
        cik = None
    key.write_text("" if cik is None else str(cik), encoding="utf-8")
    return cik


def _fts_candidates(query: str) -> list[int]:
    """EDGAR full-text-search entity autocomplete → candidate CIKs (delisted included)."""
    import requests

    key = f"fts_{query.replace('/', '_')}.json"
    j = _get_json(
        f"https://efts.sec.gov/LATEST/search-index?keysTyped={query}&hits=10", key,
        session=requests,
    )
    out: list[int] = []
    if isinstance(j, dict):
        for hit in j.get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            cik = src.get("cik") or hit.get("_id")
            try:
                out.append(int(str(cik).lstrip("0") or "0"))
            except (TypeError, ValueError):
                continue
    return out


def _ticker_in_sub(sub: dict, ticker: str) -> bool:
    subs_t = [str(t).upper() for t in (sub.get("tickers") or [])]
    t = ticker.upper()
    return t in subs_t or t.replace(".", "-") in subs_t


def build_cik_map(tickers: list[str], *, progress: Callable[[str], None] = lambda s: None) -> dict:
    """Resolve each ticker to its in-window CIK. Returns the sidecar document."""
    today_map = _today_ticker_map()
    resolved: list[dict] = []
    unresolved: list[str] = []
    for i, t in enumerate(tickers):
        if i and i % 100 == 0:
            progress(f"{i}/{len(tickers)}")
        entry: Optional[dict] = None
        cand = today_map.get(t.upper()) or today_map.get(t.upper().replace(".", "-"))
        if cand is not None:
            sub = fetch_submissions(cand)
            if _valid_for_window(sub):
                entry = {"ticker": t, "cik": cand, "method": "today_map"}
        if entry is None:
            # chase 1 (deterministic): EDGAR browse endpoint's own ticker table
            # retains delisted mappings today's company_tickers.json drops.
            bcik = _browse_edgar_cik(t)
            if bcik is not None and bcik != cand:
                sub = fetch_submissions(bcik)
                if _valid_for_window(sub):
                    entry = {"ticker": t, "cik": bcik, "method": "browse_edgar"}
        if entry is None:
            # chase 2 (fallback): FTS autocomplete on the ticker itself
            for cik in _fts_candidates(t):
                if cand is not None and cik == cand:
                    continue
                sub = fetch_submissions(cik)
                if sub and _ticker_in_sub(sub, t) and _valid_for_window(sub):
                    entry = {"ticker": t, "cik": cik, "method": "fts_predecessor"}
                    break
        if entry is None:
            unresolved.append(t)
        else:
            resolved.append(entry)
    return {
        "name": f"{UNIVERSE_NAME}_cik",
        "universe": UNIVERSE_NAME,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "provenance": (
            "PIT ticker->CIK sidecar (build-handoff SS2.1 option 1). Frozen like every "
            "universe file: once published, do not mutate. Resolution: today's "
            "company_tickers.json validated by earliest-filing-date <= window end; "
            "failures chased to predecessor CIKs via EDGAR FTS entity autocomplete "
            "(candidate must list the ticker AND predate window end). Unresolved names "
            "carry cik: null and are EXCLUDED from the trial (disclosed, never silent)."
        ),
        "n_resolved": len(resolved),
        "n_unresolved": len(unresolved),
        "unresolved": sorted(unresolved),
        "ciks": resolved,
    }


def write_sidecar(doc: dict) -> None:
    SIDECAR_PATH.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def load_sidecar() -> dict[str, int]:
    doc = yaml.safe_load(SIDECAR_PATH.read_text(encoding="utf-8"))
    return {r["ticker"]: int(r["cik"]) for r in doc.get("ciks", []) if r.get("cik")}


# ---------------------------------------------------------------------------
# §2.1 acceptance — probe coverage re-count with the PIT map
# ---------------------------------------------------------------------------


def coverage_2025q2(cikmap: dict[str, int], tickers: list[str]) -> dict:
    """Replicate the probe's C on 2025 Q2 using the PIT map. Must beat 96.5%."""
    q_lo, q_hi = date(2025, 4, 1), date(2025, 6, 30)
    covered = 0
    misses: list[str] = []
    for t in tickers:
        cik = cikmap.get(t)
        if cik is None:
            misses.append(t)
            continue
        sub = fetch_submissions(cik)
        hit = False
        if sub:
            for form, items, acc, _fd in iter_filings(sub, lo=q_lo, hi=q_hi):
                dt_et = acceptance_to_et(acc)
                if dt_et and q_lo <= dt_et.date() <= q_hi and is_earnings_filing(form, items):
                    hit = True
                    break
        if hit:
            covered += 1
        else:
            misses.append(t)
    return {"covered": covered, "total": len(tickers), "pct": covered / len(tickers) * 100,
            "misses": sorted(misses)}


# ---------------------------------------------------------------------------
# EAR computation (spec §3b alignment) + ranking + history
# ---------------------------------------------------------------------------


@dataclass
class EarningsEvent:
    ticker: str
    announce_date: str
    timing: str                       # bmo | amc | unknown
    reaction_date: str
    ear: Optional[float]              # announcement-window excess return vs SPY
    ear_rank_pct: Optional[float]     # trailing cross-sectional percentile (frozen def)
    prior_pos_ear_count: Optional[int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_ear(
    raw: dict, dates: list[date], closes: list[float],
    spy_dates: list[date], spy_closes: list[float],
) -> Optional[tuple[date, float]]:
    """(reaction_date, ear) per §3b. None if prices can't support the window."""
    ad = raw["announce_date"]
    timing = raw["timing"]
    i = bisect_right(dates, ad) - 1          # last trading day <= announce_date
    if i < 0:
        return None
    if timing == "bmo" and dates[i] == ad:
        # reaction session = announce day itself; ear = close(T-1) -> close(T)
        lo_idx, hi_idx = i - 1, i
    else:
        # AMC / unknown (conservative) / BMO on a non-trading day:
        # reaction session = next trading day; ear = close(T) -> close(T+1)
        lo_idx, hi_idx = i, i + 1
    if lo_idx < 0 or hi_idx >= len(dates):
        return None
    r = closes[hi_idx] / closes[lo_idx] - 1.0
    # SPY same calendar window
    j_lo = bisect_left(spy_dates, dates[lo_idx])
    j_hi = bisect_left(spy_dates, dates[hi_idx])
    if j_lo >= len(spy_dates) or j_hi >= len(spy_dates) \
            or spy_dates[j_lo] != dates[lo_idx] or spy_dates[j_hi] != dates[hi_idx]:
        return None
    spy_r = spy_closes[j_hi] / spy_closes[j_lo] - 1.0
    return dates[hi_idx], r - spy_r


def rank_and_history(events: list[EarningsEvent]) -> None:
    """Fill ear_rank_pct + prior_pos_ear_count in place (frozen definitions)."""
    dated = [(e, date.fromisoformat(e.reaction_date)) for e in events if e.ear is not None]
    dated.sort(key=lambda p: p[1])
    rds = [d for _, d in dated]
    ears = [p[0].ear for p in dated]
    for idx, (e, rd) in enumerate(dated):
        lo = bisect_left(rds, date.fromordinal(rd.toordinal() - RANK_TRAIL_DAYS))
        hi = bisect_left(rds, rd)              # strictly earlier reaction dates
        comp = ears[lo:hi]
        if len(comp) >= RANK_MIN_COMPARATORS:
            below = sum(1 for c in comp if c < e.ear)
            e.ear_rank_pct = round(below / len(comp) * 100.0, 2)
        else:
            e.ear_rank_pct = None
    by_ticker: dict[str, list[EarningsEvent]] = {}
    for e, _rd in dated:
        by_ticker.setdefault(e.ticker, []).append(e)
    for evs in by_ticker.values():
        for k, e in enumerate(evs):
            prior = evs[max(0, k - HISTORY_WINDOW_EVENTS):k]
            e.prior_pos_ear_count = sum(1 for p in prior if (p.ear or 0) > 0) if prior else 0


# ---------------------------------------------------------------------------
# events build + persistence
# ---------------------------------------------------------------------------


def write_events(path: Path, events: list[EarningsEvent], *, meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": "1.0",
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **meta,
        "n_events": len(events),
        "events": [e.to_dict() for e in events],
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def load_events(path: Path, *, universe: Optional[set[str]] = None) -> dict[str, list[EarningsEvent]]:
    """Load events grouped by ticker (sorted by reaction_date). Mirrors insider pattern."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    out: dict[str, list[EarningsEvent]] = {}
    for r in (doc.get("events", []) if isinstance(doc, dict) else []):
        if not isinstance(r, dict):
            continue
        tkr = str(r.get("ticker", "")).upper()
        if not tkr or (universe is not None and tkr not in universe):
            continue
        try:
            ev = EarningsEvent(
                ticker=tkr,
                announce_date=str(r["announce_date"]),
                timing=str(r.get("timing", "unknown")),
                reaction_date=str(r["reaction_date"]),
                ear=None if r.get("ear") is None else float(r["ear"]),
                ear_rank_pct=None if r.get("ear_rank_pct") is None else float(r["ear_rank_pct"]),
                prior_pos_ear_count=None if r.get("prior_pos_ear_count") is None else int(r["prior_pos_ear_count"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        out.setdefault(tkr, []).append(ev)
    for evs in out.values():
        evs.sort(key=lambda e: e.reaction_date)
    return out


def _price_series(ticker: str) -> Optional[tuple[list[date], list[float]]]:
    from tools.backtest import data_cache

    try:
        df = data_cache.load(ticker)
    except Exception:  # noqa: BLE001
        return None
    if df is None or len(df) == 0 or "Close" not in df.columns:
        return None
    ds = [ix.date() for ix in df.index]
    return ds, [float(c) for c in df["Close"].values]


def ensure_price_history(tickers: list[str], *, progress: Callable[[str], None] = lambda s: None) -> None:
    """Selective refetch: extend any cache that starts after the 2016 warm-up."""
    from tools.backtest import data_cache

    need = []
    for t in tickers:
        try:
            df = data_cache.load(t)
            if df is None or len(df) == 0 or df.index[0].date() > date(2016, 1, 5):
                need.append(t)
        except Exception:  # noqa: BLE001
            need.append(t)
    progress(f"{len(need)} tickers need 2016+ refetch")
    for i, t in enumerate(need):
        if i and i % 50 == 0:
            progress(f"refetch {i}/{len(need)}")
        try:
            data_cache.fetch(t, start=WINDOW_START, end=date.today(), force_refetch=True)
        except Exception as exc:  # noqa: BLE001
            progress(f"{t}: refetch failed - {exc!r}")


def build_events(cikmap: dict[str, int], tickers: list[str],
                 *, progress: Callable[[str], None] = lambda s: None) -> list[EarningsEvent]:
    spy = _price_series("SPY")
    if spy is None:
        raise RuntimeError("SPY prices unavailable — cannot compute excess returns")
    spy_dates, spy_closes = spy
    events: list[EarningsEvent] = []
    n_no_prices = 0
    for i, t in enumerate(tickers):
        if i and i % 100 == 0:
            progress(f"{i}/{len(tickers)} — {len(events)} events")
        cik = cikmap.get(t)
        if cik is None:
            continue
        sub = fetch_submissions(cik)
        if not sub:
            continue
        raws = extract_raw_events(sub, t)
        if not raws:
            continue
        px = _price_series(t)
        for raw in raws:
            if px is None:
                n_no_prices += 1
                events.append(EarningsEvent(t, raw["announce_date"].isoformat(), raw["timing"],
                                            raw["announce_date"].isoformat(), None, None, None))
                continue
            got = compute_ear(raw, px[0], px[1], spy_dates, spy_closes)
            if got is None:
                events.append(EarningsEvent(t, raw["announce_date"].isoformat(), raw["timing"],
                                            raw["announce_date"].isoformat(), None, None, None))
            else:
                rd, ear = got
                events.append(EarningsEvent(t, raw["announce_date"].isoformat(), raw["timing"],
                                            rd.isoformat(), round(ear, 6), None, None))
    rank_and_history(events)
    progress(f"done: {len(events)} events ({n_no_prices} without prices)")
    return events


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _universe_tickers() -> list[str]:
    from tools.quant_strategies._universe import get_universe

    return [t for t in get_universe(UNIVERSE_NAME) if t != "SPY"]


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Earnings-events pipeline (EDGAR-only, frozen filter)")
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("cikmap", help="build + freeze the PIT ticker->CIK sidecar")
    sp.add_parser("coverage", help="SS2.1 acceptance: 2025-Q2 coverage with the PIT map")
    sp.add_parser("events", help="build the events YAML (frozen filter, 2016-2025)")
    args = p.parse_args()
    tickers = _universe_tickers()

    if args.cmd == "cikmap":
        if SIDECAR_PATH.exists():
            print(f"REFUSING to overwrite frozen sidecar {SIDECAR_PATH} — delete manually if truly intended.")
            return
        doc = build_cik_map(tickers, progress=lambda s: print(f"  {s}", flush=True))
        write_sidecar(doc)
        print(f"resolved {doc['n_resolved']}/{len(tickers)}; unresolved {doc['n_unresolved']}: {doc['unresolved']}")
        print(f"wrote {SIDECAR_PATH}")
    elif args.cmd == "coverage":
        cikmap = load_sidecar()
        r = coverage_2025q2(cikmap, tickers)
        print(f"2025-Q2 coverage with PIT map: {r['covered']}/{r['total']} = {r['pct']:.1f}% "
              f"(probe floor was 96.5% — {'RISES: acceptance PASS' if r['pct'] > 96.5 else 'DOES NOT RISE: STOP'})")
        print(f"misses ({len(r['misses'])}): {r['misses']}")
    elif args.cmd == "events":
        cikmap = load_sidecar()
        ensure_price_history([t for t in tickers if t in cikmap] + ["SPY"],
                             progress=lambda s: print(f"  {s}", flush=True))
        events = build_events(cikmap, tickers, progress=lambda s: print(f"  {s}", flush=True))
        write_events(EVENTS_PATH, events, meta={
            "universe": UNIVERSE_NAME,
            "cik_sidecar": str(SIDECAR_PATH.relative_to(_ROOT)),
            "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
            "filter": "FROZEN strict 8-K item 2.02 (2026-08-06); see ledgers/improvements/2026-08-06-earnings-drift-frozen-filter.md",
        })
        n_ear = sum(1 for e in events if e.ear is not None)
        n_ranked = sum(1 for e in events if e.ear_rank_pct is not None)
        unk = sum(1 for e in events if e.timing == "unknown")
        print(f"{len(events)} events -> {EVENTS_PATH}")
        print(f"with ear: {n_ear} · ranked: {n_ranked} · unknown-timing share: {unk/max(len(events),1)*100:.1f}%")


if __name__ == "__main__":
    main()
