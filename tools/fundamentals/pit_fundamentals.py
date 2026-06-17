"""Point-in-time (PIT) fundamentals from the SEC XBRL company-facts API.

This is the reusable financial-statement layer for the whole fundamental
factor roster (value, quality, profitability) — not just one KIND. It pulls
``data.sec.gov/api/xbrl/companyfacts/CIK##########.json`` (free) and extracts,
per company, the *as-filed* time series of each us-gaap / dei concept we need:

  book equity, shares outstanding, net income, revenue, COGS, total assets,
  operating cash flow, capex  (FCF = OCF - capex).

POINT-IN-TIME DISCIPLINE (load-bearing — this is the whole point of the layer)
-----------------------------------------------------------------------------
Every datapoint in company-facts carries a ``filed`` date (the date the filing
was public on EDGAR). We treat that as ``available_at`` and NEVER use a value
before its filing was public. At daily-bar backtest granularity ``filed`` (date)
is look-ahead-safe — a value filed on day D is first usable on the next bar,
exactly like ``form345_bulk``'s FILING_DATE convention. (True intraday
acceptanceDateTime would need a second submissions-API call per accession; not
worth it for daily bars, and ``filed`` is already conservative.)

We use AS-FILED vintages, never restated/latest: ``ttm_flow_as_of`` and
``latest_stock_as_of`` only ever consider datapoints with ``filed <= asof``, so
a later restatement of an old period is invisible until it too is filed.

SURVIVORSHIP: this layer is survivorship-clean for the *names you ask about*
(it reads each CIK's full as-filed history). It does NOT fix a survivor-biased
*universe* list — if delisted tickers aren't in your universe YAML, that bias
lives upstream. Flagged, not silently absorbed.

SEC etiquette: declared User-Agent on every request + a 10 req/s throttle.

CLI::

    uv run python -m tools.fundamentals.pit_fundamentals --ticker AAPL --asof 2024-06-30
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

TOOL = "tools/fundamentals/pit_fundamentals.py"

EDGAR_IDENTITY_ENV = "EDGAR_IDENTITY"
DEFAULT_IDENTITY = "Bertrand Shin shinbertrand@gmail.com"

_BASE = Path(__file__).resolve().parents[2]
CACHE_DIR_DEFAULT = _BASE / "tools" / "cache" / "fundamentals_pit"
TICKER_MAP_PATH = CACHE_DIR_DEFAULT / "_company_tickers.json"

COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# company-facts is updated as new filings land, but a given (concept, end,
# accn) datapoint is immutable. 24h TTL keeps the on-disk copy current without
# re-fetching mid-run. Past as-of queries are deterministic regardless.
CACHE_TTL_SECONDS = 86400

# SEC asks for <= 10 req/s. We serialise SEC HTTP behind a lock + min-interval.
_MIN_REQUEST_INTERVAL = 0.12
_throttle_lock = threading.Lock()
_last_request = [0.0]

# Concept fallback chains. First concept that yields usable points wins.
# (namespace, concept_name) — dei carries the cover-page share count.
BOOK_EQUITY = [
    ("us-gaap", "StockholdersEquity"),
    ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
]
SHARES = [
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesOutstanding"),
    ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
    ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
]
NET_INCOME = [
    ("us-gaap", "NetIncomeLoss"),
    ("us-gaap", "ProfitLoss"),
]
REVENUE = [
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("us-gaap", "Revenues"),
    ("us-gaap", "SalesRevenueNet"),
]
COGS = [
    ("us-gaap", "CostOfGoodsAndServicesSold"),
    ("us-gaap", "CostOfRevenue"),
    ("us-gaap", "CostOfGoodsSold"),
]
ASSETS = [
    ("us-gaap", "Assets"),
]
OCF = [
    ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
    ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
]
CAPEX = [
    ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
    ("us-gaap", "PaymentsToAcquireProductiveAssets"),
    ("us-gaap", "PaymentsForCapitalImprovements"),
]


class PitFundamentalsError(Exception):
    """Raised when SEC returns no usable data for a ticker/CIK."""


@dataclass(frozen=True)
class FactPoint:
    """One as-filed XBRL datapoint.

    ``end`` is the period-end (instant for stock concepts). ``start`` is None
    for stock concepts. ``filed`` is the availability date (PIT key).
    """
    concept: str
    val: float
    end: date
    filed: date
    start: Optional[date] = None
    form: str = ""
    fp: Optional[str] = None
    fy: Optional[int] = None
    accn: str = ""

    @property
    def period_days(self) -> Optional[int]:
        if self.start is None:
            return None
        return (self.end - self.start).days

    @property
    def is_quarter(self) -> bool:
        pd_ = self.period_days
        return pd_ is not None and 60 <= pd_ <= 100

    @property
    def is_annual(self) -> bool:
        pd_ = self.period_days
        return pd_ is not None and 330 <= pd_ <= 400


@dataclass
class Fundamentals:
    """PIT fundamentals snapshot for one ticker as of one date.

    Flow fields (net_income, revenue, cogs, ocf, capex) are trailing-twelve-
    month. Stock fields (book_equity, shares, total_assets) are the latest
    as-filed instant. ``fcf = ocf - capex``. Any field is None when no usable
    as-filed datapoint existed on/before ``asof``.
    """
    ticker: str
    asof: str                       # ISO date
    book_equity: Optional[float] = None
    shares: Optional[float] = None
    total_assets: Optional[float] = None
    ttm_net_income: Optional[float] = None
    ttm_revenue: Optional[float] = None
    ttm_cogs: Optional[float] = None
    ttm_ocf: Optional[float] = None
    ttm_capex: Optional[float] = None
    fcf: Optional[float] = None
    provenance: dict = field(default_factory=dict)   # field -> {filed, end, concept}
    fetched_at: str = ""
    source: str = "data.sec.gov/api/xbrl/companyfacts"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Network layer (injectable for offline tests)                                #
# --------------------------------------------------------------------------- #
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _identity() -> str:
    import os
    return os.environ.get(EDGAR_IDENTITY_ENV) or DEFAULT_IDENTITY


def _http_get_json(url: str) -> Any:
    """Throttled GET with a declared User-Agent. Serialised to <= ~8 req/s."""
    with _throttle_lock:
        wait = _MIN_REQUEST_INTERVAL - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": _identity(),
            "Accept-Encoding": "gzip, deflate",
            "Host": urllib.parse.urlparse(url).netloc,
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        finally:
            _last_request[0] = time.time()


FactsFetcher = Callable[[str], Any]


def _default_facts_fetcher(cik: str) -> Any:
    return _http_get_json(COMPANY_FACTS_URL.format(cik=cik))


# --------------------------------------------------------------------------- #
# Ticker -> CIK                                                               #
# --------------------------------------------------------------------------- #
def load_ticker_cik_map(
    *,
    fetcher: Optional[Callable[[], Any]] = None,
    cache_path: Optional[Path] = None,
    use_cache: bool = True,
) -> dict[str, str]:
    """{TICKER -> zero-padded 10-digit CIK}. Cached to disk (refreshed past TTL)."""
    cache_path = cache_path or TICKER_MAP_PATH
    if use_cache and cache_path.is_file():
        try:
            data = json.loads(cache_path.read_text())
            if (time.time() - data.get("_cached_at_epoch", 0)) <= CACHE_TTL_SECONDS:
                return data["map"]
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    raw = (fetcher() if fetcher else _http_get_json(COMPANY_TICKERS_URL))
    out: dict[str, str] = {}
    # company_tickers.json is {"0": {"cik_str": 320193, "ticker": "AAPL", ...}, ...}
    rows = raw.values() if isinstance(raw, dict) else raw
    for row in rows:
        tkr = str(row.get("ticker", "")).upper().strip()
        cik = row.get("cik_str")
        if tkr and cik is not None:
            out[tkr] = f"{int(cik):010d}"
    if use_cache and out:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(
            {"_cached_at_epoch": time.time(), "_cached_at_iso": _utc_now_iso(), "map": out}))
    return out


def ticker_to_cik(ticker: str, **kw) -> Optional[str]:
    return load_ticker_cik_map(**kw).get(ticker.upper().strip())


# --------------------------------------------------------------------------- #
# company-facts fetch + caching                                              #
# --------------------------------------------------------------------------- #
def fetch_company_facts(
    cik: str,
    *,
    fetcher: Optional[FactsFetcher] = None,
    cache_dir: Optional[Path] = None,
    use_cache: bool = True,
) -> dict:
    cik = f"{int(cik):010d}"
    cache_dir = cache_dir or CACHE_DIR_DEFAULT
    cpath = cache_dir / f"CIK{cik}.json"
    if use_cache and cpath.is_file():
        try:
            data = json.loads(cpath.read_text())
            if (time.time() - data.get("_cached_at_epoch", 0)) <= CACHE_TTL_SECONDS:
                return data["facts"]
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    facts = (fetcher or _default_facts_fetcher)(cik)
    if not isinstance(facts, dict):
        raise PitFundamentalsError(f"company-facts for CIK{cik} not a dict")
    if use_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cpath.write_text(json.dumps(
            {"_cached_at_epoch": time.time(), "_cached_at_iso": _utc_now_iso(), "facts": facts}))
    return facts


# --------------------------------------------------------------------------- #
# Concept extraction + PIT accessors (pure — fully testable offline)          #
# --------------------------------------------------------------------------- #
def _parse_date(s: Any) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def extract_points(facts: dict, chain: Sequence[tuple[str, str]]) -> list[FactPoint]:
    """All as-filed datapoints for the first concept in ``chain`` that has any.

    Concatenates across every unit (USD / shares / etc.) since a concept lives
    under exactly one unit type in practice. Returns sorted by (end, filed).
    """
    facts_block = facts.get("facts", facts)  # accept raw or {"facts": ...}
    for ns, concept in chain:
        units = (facts_block.get(ns, {}).get(concept, {}) or {}).get("units", {})
        points: list[FactPoint] = []
        for _unit, rows in units.items():
            for r in rows:
                end = _parse_date(r.get("end"))
                filed = _parse_date(r.get("filed"))
                val = r.get("val")
                if end is None or filed is None or val is None:
                    continue
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    continue
                points.append(FactPoint(
                    concept=f"{ns}:{concept}", val=val, end=end, filed=filed,
                    start=_parse_date(r.get("start")), form=str(r.get("form", "")),
                    fp=r.get("fp"), fy=r.get("fy"), accn=str(r.get("accn", "")),
                ))
        if points:
            points.sort(key=lambda p: (p.end, p.filed))
            return points
    return []


def latest_stock_as_of(points: list[FactPoint], asof: date) -> Optional[FactPoint]:
    """Most recent instant value with ``filed <= asof`` (latest end, then filed)."""
    avail = [p for p in points if p.filed <= asof]
    if not avail:
        return None
    return max(avail, key=lambda p: (p.end, p.filed))


def _shift_back_one_year(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:               # Feb 29
        return d.replace(year=d.year - 1, day=28)


def _ttm_from_discrete_quarters(avail: list[FactPoint]) -> Optional[FactPoint]:
    """TTM by summing 4 trailing discrete 3-month quarters (income-statement
    style). Derives Q4 = FY - (the 3 interior quarters) when present."""
    q_by_end: dict[date, FactPoint] = {}
    for p in sorted((p for p in avail if p.is_quarter), key=lambda x: x.filed):
        q_by_end[p.end] = p
    annuals = sorted((p for p in avail if p.is_annual), key=lambda x: (x.end, x.filed))

    for a in annuals:
        if a.start is None:
            continue
        interior = [q for q in q_by_end.values()
                    if q.start is not None and q.start >= a.start and q.end <= a.end]
        if len(interior) == 3 and a.end not in q_by_end:
            last_q_end = max(q.end for q in interior)
            q_by_end[a.end] = FactPoint(
                concept=a.concept + ":Q4derived", val=a.val - sum(q.val for q in interior),
                end=a.end, filed=a.filed, start=last_q_end, form=a.form, fp="Q4",
                fy=a.fy, accn=a.accn)

    quarters = sorted(q_by_end.values(), key=lambda p: p.end)
    if len(quarters) < 4:
        return None
    last4 = quarters[-4:]
    span = (last4[-1].end - last4[0].start).days if last4[0].start else None
    if span is None or not (300 <= span <= 430):   # rejects 4-of-same-quarter
        return None
    newest = last4[-1]
    return FactPoint(
        concept=newest.concept.split(":Q4derived")[0] + ":TTM",
        val=float(sum(q.val for q in last4)),
        end=newest.end, filed=max(q.filed for q in last4),
        start=last4[0].start, form="TTM", fp="TTM", fy=newest.fy, accn=newest.accn)


def _ttm_rolling_cumulative(avail: list[FactPoint]) -> Optional[FactPoint]:
    """TTM = prior-FY + current-YTD - prior-year-YTD (cash-flow style).

    Cash-flow concepts are reported as cumulative year-to-date interims
    (90/180/270d) plus a 365d FY. The trailing 12 months ending at the latest
    interim end E is: FY(ending in (E-1yr, E)) + YTD(end=E) - YTD(end≈E-1yr).
    """
    interims = [p for p in avail if p.period_days and 120 <= p.period_days < 330]
    if not interims:
        return None
    latest = max(interims, key=lambda p: (p.end, p.period_days, p.filed))
    E, L = latest.end, latest.period_days
    annuals = [p for p in avail if p.is_annual and p.end < E]
    if not annuals:
        return None
    A = max(annuals, key=lambda p: (p.end, p.filed))
    target = _shift_back_one_year(E)
    prior = [p for p in interims
             if abs((p.end - target).days) <= 25 and abs((p.period_days or 0) - L) <= 25]
    if not prior:
        return None
    py = min(prior, key=lambda p: abs((p.end - target).days))
    return FactPoint(
        concept=latest.concept + ":TTMroll", val=float(A.val + latest.val - py.val),
        end=E, filed=max(A.filed, latest.filed, py.filed),
        start=_shift_back_one_year(E), form="TTM", fp="TTM", fy=latest.fy, accn=latest.accn)


def ttm_flow_as_of(points: list[FactPoint], asof: date) -> Optional[FactPoint]:
    """Trailing-twelve-month value of a flow concept, as-filed on/before ``asof``.

    Layered (best freshness first, all strictly PIT via ``filed <= asof``):
      1. discrete 4-quarter sum (income-statement reporting),
      2. FY + YTD - prior-YTD rolling (cumulative cash-flow reporting),
      3. latest annual datapoint (sparse / annual-only filers).
    """
    avail = [p for p in points if p.filed <= asof and p.val is not None]
    if not avail:
        return None
    for method in (_ttm_from_discrete_quarters, _ttm_rolling_cumulative):
        got = method(avail)
        if got is not None:
            return got
    annuals = sorted((p for p in avail if p.is_annual), key=lambda x: (x.end, x.filed))
    if annuals:
        a = annuals[-1]
        return FactPoint(concept=a.concept + ":FY", val=a.val, end=a.end,
                         filed=a.filed, start=a.start, form=a.form, fp="FY",
                         fy=a.fy, accn=a.accn)
    return None


# --------------------------------------------------------------------------- #
# Top-level snapshot                                                          #
# --------------------------------------------------------------------------- #
def fundamentals_as_of(
    ticker: str,
    asof: date,
    *,
    facts: Optional[dict] = None,
    fetcher: Optional[FactsFetcher] = None,
    cache_dir: Optional[Path] = None,
    use_cache: bool = True,
    cik_map: Optional[dict[str, str]] = None,
) -> Optional[Fundamentals]:
    """PIT fundamentals for ``ticker`` as of ``asof``.

    Pass ``facts`` (a raw company-facts dict) to skip the network entirely —
    used in tests and by the cache-prewarm path. Otherwise resolves CIK and
    fetches (disk-cached). Returns None when the CIK can't be resolved.
    """
    ticker = ticker.upper().strip()
    if facts is None:
        cik = (cik_map or {}).get(ticker) or ticker_to_cik(
            ticker, cache_path=(cache_dir or CACHE_DIR_DEFAULT) / "_company_tickers.json"
            if cache_dir else None)
        if not cik:
            return None
        try:
            facts = fetch_company_facts(cik, fetcher=fetcher, cache_dir=cache_dir,
                                        use_cache=use_cache)
        except (PitFundamentalsError, OSError, ValueError):
            return None

    prov: dict[str, dict] = {}

    def _stock(chain) -> Optional[float]:
        p = latest_stock_as_of(extract_points(facts, chain), asof)
        if p is None:
            return None
        prov[chain[0][1]] = {"filed": p.filed.isoformat(), "end": p.end.isoformat(),
                             "concept": p.concept}
        return p.val

    def _ttm(chain) -> Optional[float]:
        p = ttm_flow_as_of(extract_points(facts, chain), asof)
        if p is None:
            return None
        prov[chain[0][1]] = {"filed": p.filed.isoformat(), "end": p.end.isoformat(),
                             "concept": p.concept}
        return p.val

    book = _stock(BOOK_EQUITY)
    shares = _stock(SHARES)
    assets = _stock(ASSETS)
    ni = _ttm(NET_INCOME)
    rev = _ttm(REVENUE)
    cogs = _ttm(COGS)
    ocf = _ttm(OCF)
    capex = _ttm(CAPEX)
    fcf = (ocf - capex) if (ocf is not None and capex is not None) else None

    return Fundamentals(
        ticker=ticker, asof=asof.isoformat(),
        book_equity=book, shares=shares, total_assets=assets,
        ttm_net_income=ni, ttm_revenue=rev, ttm_cogs=cogs,
        ttm_ocf=ocf, ttm_capex=capex, fcf=fcf,
        provenance=prov, fetched_at=_utc_now_iso(),
    )


def main() -> None:
    p = argparse.ArgumentParser(prog="tools.fundamentals.pit_fundamentals")
    p.add_argument("--ticker", required=True)
    p.add_argument("--asof", required=True, help="YYYY-MM-DD")
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args()
    res = fundamentals_as_of(args.ticker, date.fromisoformat(args.asof),
                             use_cache=not args.no_cache)
    print(json.dumps(res.to_dict() if res else {"error": "no data"}, indent=2))


if __name__ == "__main__":
    main()
