"""EDGAR coverage probe — measures C (8-K item-2.02 coverage) + A (ambiguous timestamps)
for the liquid_us universe over 2025 Q2. Decides EODHD vs EDGAR-only per the
PRE-REGISTERED rule in 2026-08-06-edgar-coverage-probe.md (written before this ran).

Read-only against EDGAR (data.sec.gov). NOT a trial — no pipeline, no trials.yml, no grid.
Throwaway probe per handoff convention; lives in ledgers/improvements/.

Method: one bulk ticker->CIK map (company_tickers.json), then per-name submissions JSON.
A name is COVERED if >=1 filing with form 8-K/8-K/A, items containing "2.02", and a
parseable acceptanceDateTime inside 2025-04-01..2025-06-30 (ET). acceptanceDateTime is
UTC in the JSON; converted to US/Eastern for the BMO/AMC split (BMO <09:30, AMC >=16:00,
else ambiguous). Heavy filers whose `recent` window doesn't reach the quarter get their
older submission pages fetched. Misses are split: 6-K-in-window (FPI structural) /
no-filings-in-window (likely delisted/acquired residual) / other (true miss).

Usage:  uv run python ledgers/improvements/2026-08-06-edgar-coverage-probe.py
"""
from __future__ import annotations
import os, sys, json, time, collections
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from datetime import datetime, date
from zoneinfo import ZoneInfo
import requests

from tools.quant_strategies._universe import resolve_universe_tickers
from tools.fundamentals.edgar_eps import EDGAR_IDENTITY_ENV, DEFAULT_IDENTITY

UNIVERSE_SPEC = {"universe": {"name": "liquid_us_2026q2", "benchmark": "SPY"}}
Q_START, Q_END = date(2025, 4, 1), date(2025, 6, 30)
ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
UA = os.environ.get(EDGAR_IDENTITY_ENV) or DEFAULT_IDENTITY
HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
SLEEP = 0.12          # ~8 req/s, under SEC's 10/s guidance
OUT_MD = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-06-edgar-coverage-probe.md")
CACHE = os.path.join(_ROOT, "ledgers", "improvements", "_edgar_probe_cache")
os.makedirs(CACHE, exist_ok=True)

session = requests.Session()
session.headers.update(HEADERS)


def get_json(url, cache_key):
    p = os.path.join(CACHE, cache_key)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                j = r.json()
                with open(p, "w", encoding="utf-8") as fh:
                    json.dump(j, fh)
                time.sleep(SLEEP)
                return j
            if r.status_code == 404:
                time.sleep(SLEEP)
                return None
            time.sleep(1.5 * (attempt + 1))
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return None


def to_et(acc: str):
    """SEC submissions acceptanceDateTime ('2025-04-30T20:05:12.000Z', UTC) -> ET datetime."""
    if not acc:
        return None
    try:
        s = acc.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(ET)
    except (ValueError, TypeError):
        return None


def iter_filings(cik10, sub):
    """Yield (form, items, acceptanceDateTime) across recent + any older pages overlapping Q."""
    recent = sub.get("filings", {}).get("recent", {})
    yield from zip(recent.get("form", []), recent.get("items", [""] * len(recent.get("form", []))),
                   recent.get("acceptanceDateTime", []))
    # if `recent` doesn't reach back to Q_START, pull older pages that overlap the window
    dates = recent.get("filingDate", [])
    reaches_back = bool(dates) and min(dates) <= Q_START.isoformat()
    if not reaches_back:
        for f in sub.get("filings", {}).get("files", []):
            if f.get("filingTo", "") < Q_START.isoformat() and f.get("filingTo", "") != "":
                pass  # entirely before window is fine to include; cheap safeguard below filters
            if f.get("filingFrom", "9999") > Q_END.isoformat():
                continue
            if f.get("filingTo", "0000") < Q_START.isoformat():
                continue
            j = get_json(f"https://data.sec.gov/submissions/{f['name']}", f["name"])
            if not j:
                continue
            yield from zip(j.get("form", []), j.get("items", [""] * len(j.get("form", []))),
                           j.get("acceptanceDateTime", []))


def classify(dt_et):
    t = dt_et.time()
    if t < datetime(2000, 1, 1, 9, 30).time():
        return "BMO"
    if t >= datetime(2000, 1, 1, 16, 0).time():
        return "AMC"
    return "ambiguous"


def main():
    tickers = [t for t in resolve_universe_tickers(UNIVERSE_SPEC) if t != "SPY"]
    print(f"universe liquid_us_2026q2: {len(tickers)} names (SPY excluded)", flush=True)

    tmap_j = get_json("https://www.sec.gov/files/company_tickers.json", "company_tickers.json")
    tick2cik = {}
    for _, row in (tmap_j or {}).items():
        tick2cik[str(row["ticker"]).upper()] = int(row["cik_str"])

    covered = {}          # ticker -> (class, dt_et) of best in-window 2.02 8-K
    hour_hist = collections.Counter()
    miss_no_cik, miss_fpi_6k, miss_no_filings, miss_other = [], [], [], []
    n_pages_extra = 0

    for i, t in enumerate(tickers):
        if i and i % 100 == 0:
            print(f"  {i}/{len(tickers)} ... covered so far {len(covered)}", flush=True)
        cik = tick2cik.get(t.upper()) or tick2cik.get(t.upper().replace(".", "-"))
        if cik is None:
            miss_no_cik.append(t)
            continue
        cik10 = f"{cik:010d}"
        sub = get_json(f"https://data.sec.gov/submissions/CIK{cik10}.json", f"CIK{cik10}.json")
        if sub is None:
            miss_other.append((t, "submissions fetch failed"))
            continue
        best = None
        saw_6k_in_window = False
        saw_any_in_window = False
        for form, items, acc in iter_filings(cik10, sub):
            dt_et = to_et(acc)
            if dt_et is None:
                continue
            d = dt_et.date()
            if not (Q_START <= d <= Q_END):
                continue
            saw_any_in_window = True
            if form in ("6-K", "6-K/A"):
                saw_6k_in_window = True
            if form in ("8-K", "8-K/A") and "2.02" in (items or ""):
                if best is None or dt_et < best:
                    best = dt_et
        if best is not None:
            cls = classify(best)
            covered[t] = (cls, best)
            hour_hist[best.hour] += 1
        elif saw_6k_in_window:
            miss_fpi_6k.append(t)
        elif not saw_any_in_window:
            miss_no_filings.append(t)
        else:
            miss_other.append((t, "filings in window but no 2.02 8-K"))

    N = len(tickers)
    n_cov = len(covered)
    C = n_cov / N
    cls_counts = collections.Counter(c for c, _ in covered.values())
    A = cls_counts["ambiguous"] / n_cov if n_cov else 0.0

    # legitimate residuals = no CIK (delisted/renamed) + no filings at all in window
    n_residual = len(miss_no_cik) + len(miss_no_filings)
    C_adj = n_cov / max(N - n_residual, 1)

    # pre-registered decision rule
    if C >= 0.95 and A <= 0.05:
        decision = "EDGAR-only. EODHD question CLOSED permanently."
    elif (0.90 <= C < 0.95) or (0.05 < A <= 0.10):
        decision = "EDGAR-only, with gap logged + clustering check (see findings)."
    else:
        decision = "Buy ONE month of EODHD (~US$20-30) — operator action, reported not taken."

    lines = ["", "## RESULTS (probe ran after the pre-registration above)", "",
             f"- Universe: `liquid_us_2026q2` via `resolve_universe_tickers` — **{N} names** (SPY excluded). "
             f"Quarter: filings accepted {Q_START} → {Q_END} (ET).",
             f"- Ticker→CIK map: SEC `company_tickers.json`; {len(miss_no_cik)} names unmapped.", "",
             "### C — coverage",
             f"- Covered (≥1 8-K w/ item 2.02 + parseable acceptanceDateTime in-window): **{n_cov}/{N} = {C*100:.1f}%**",
             f"- Legitimate residuals separated out: **{n_residual}** "
             f"(no CIK mapping: {len(miss_no_cik)}; zero EDGAR filings of any form in-window: {len(miss_no_filings)})",
             f"- Coverage excluding residuals: **{n_cov}/{N - n_residual} = {C_adj*100:.1f}%**",
             f"- FPI/6-K structural (filed 6-K in-window, no 2.02 8-K): **{len(miss_fpi_6k)}** — {sorted(miss_fpi_6k) or '—'}",
             f"- Other misses (**true misses** — filings in-window but no 2.02 8-K): **{len(miss_other)}**", ""]
    if miss_other:
        lines.append("  | ticker | note |")
        lines.append("  |---|---|")
        for t, why in sorted(miss_other):
            lines.append(f"  | {t} | {why} |")
        lines.append("")
    if miss_no_cik:
        lines.append(f"- No-CIK list: {sorted(miss_no_cik)}")
    if miss_no_filings:
        lines.append(f"- Zero-filings list (likely delisted/acquired): {sorted(miss_no_filings)}")
    lines += ["", "### A — timestamp usability (classifying filing per covered name, ET)",
              f"- BMO (<09:30): **{cls_counts['BMO']}** ({cls_counts['BMO']/max(n_cov,1)*100:.1f}%)",
              f"- AMC (>=16:00): **{cls_counts['AMC']}** ({cls_counts['AMC']/max(n_cov,1)*100:.1f}%)",
              f"- Ambiguous (09:30–16:00): **{cls_counts['ambiguous']}** → **A = {A*100:.1f}%**", "",
              "Hour-of-day histogram (ET, acceptance hour of the classifying filing):", "",
              "| hour | n | | hour | n |", "|---|---|---|---|---|"]
    for h in range(0, 12):
        lines.append(f"| {h:02d} | {hour_hist.get(h, 0)} | | {h+12:02d} | {hour_hist.get(h+12, 0)} |")
    lines += ["", "### Decision (per the PRE-REGISTERED rule)",
              f"- C = {C*100:.1f}% · A = {A*100:.1f}% → **{decision}**", ""]
    with open(OUT_MD, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"\nC={C*100:.1f}% ({n_cov}/{N}; adj {C_adj*100:.1f}% excl {n_residual} residuals)")
    print(f"A={A*100:.1f}%  BMO={cls_counts['BMO']} AMC={cls_counts['AMC']} ambiguous={cls_counts['ambiguous']}")
    print(f"FPI/6-K structural: {len(miss_fpi_6k)}  true-miss: {len(miss_other)}")
    print(f"DECISION: {decision}")
    print(f"appended results to {OUT_MD}")


if __name__ == "__main__":
    main()
