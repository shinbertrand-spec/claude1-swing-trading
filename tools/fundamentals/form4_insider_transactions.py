"""SEC Form 4 open-market insider-purchase ingest (edgartools).

Foundation layer for the opportunistic-insider-buying KIND. This module does
ONE thing: turn raw SEC Form 4 filings into a clean, de-duplicated, cached list
of **open-market purchase** transactions (``transactionCode == "P"``,
``acquired_disposed == "A"``). Classification, conviction scoring, and the
trading KIND itself are built on top in later phases — none of that lives here.

Design commitments (load-bearing):

* **Anti-look-ahead — key every event off ``acceptanceDateTime``, NEVER
  ``filingDate``.** A Form 4 reports a transaction that already happened (the
  ``period_of_report`` / transaction date), but the information only becomes
  public when the SEC *accepts* the filing. ``acceptance_datetime`` carries the
  exact timestamp (e.g. ``2026-06-15 16:50:47`` — after the close), which lets a
  backtest decide the first session it could have acted on. We store the full
  acceptance timestamp AND a derived ``event_date`` (the acceptance calendar
  date); downstream replay enters at the *next* bar after ``event_date``, which
  is look-ahead-safe whether the filing was accepted intraday or after hours.

* **Purchases only.** ``transaction_code == "P"`` with ``acquired_disposed ==
  "A"``. Sales (S), gifts (G), option exercises (M), awards (A-code), and tax
  withholdings (F) are dropped — they carry no discretionary-conviction signal.

* **Best-effort 10b5-1 flag.** Pre-scheduled 10b5-1 plan purchases are not
  discretionary signal. The Form 4 XML carries a document-level checkbox that
  edgartools 5.31.5 does not surface, so we fall back to a footnote text scan
  for "10b5-1" / "10b5-1(c)". ``is_10b5_1`` is advisory; the caller decides
  whether to drop. (Plan *purchases* are rare vs plan sales, so the false-miss
  rate is low.)

* **Cache is the speed story.** There is no index-level "is this a purchase"
  filter — finding the ~5-15% of daily Form 4s that are open-market buys means
  parsing each one's XML. That is inherently network-heavy (a day can carry
  ~2,000 Form 4s). We cache the parsed result per UTC/ET day; past days are
  immutable so their cache never expires.

Identity: SEC requires every API caller to send an identity header. Set
``EDGAR_IDENTITY`` env var (e.g. ``"Bertrand Shin shinbertrand@gmail.com"``);
falls back to :data:`DEFAULT_IDENTITY`.

CLI::

    uv run python -m tools.fundamentals.form4_insider_transactions --date 2026-06-15 --limit 200
    uv run python -m tools.fundamentals.form4_insider_transactions --current
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from ..cli import emit
from ..contract import TraceEntry

TOOL = "tools/fundamentals/form4_insider_transactions.py"

EDGAR_IDENTITY_ENV = "EDGAR_IDENTITY"
DEFAULT_IDENTITY = "Bertrand Shin shinbertrand@gmail.com"

CACHE_DIR_DEFAULT = Path(__file__).resolve().parents[2] / "tools" / "cache" / "form4"
# Past trading days are immutable, so their cache never expires. "Today" (in ET,
# the SEC's filing timezone) keeps accepting filings until ~22:00 ET, so its
# cache is short-lived.
TODAY_CACHE_TTL_SECONDS = 3600  # 1h

ET = ZoneInfo("America/New_York")

# Transaction codes we keep: open-market / private purchase, acquisition side.
PURCHASE_CODE = "P"
ACQUIRED = "A"

_identity_set = False


class Form4IngestError(Exception):
    """Raised when Form 4 ingest cannot produce a usable result."""


@dataclass
class InsiderPurchase:
    """One open-market insider purchase line item (one row of one Form 4).

    Every field is JSON-serialisable. Numeric fields are plain ``float`` / ``int``
    (numpy scalars from edgartools are coerced at parse time).
    """

    accession_number: str
    issuer_cik: str
    ticker: str | None
    issuer_name: str
    insider_name: str
    insider_cik: str
    position: str | None
    is_director: bool
    is_officer: bool
    is_ten_pct_owner: bool
    is_other: bool
    officer_title: str | None
    transaction_date: str           # transaction date (period of report), ISO date
    acceptance_datetime: str        # SEC acceptance timestamp, ISO — THE EVENT KEY
    event_date: str                 # acceptance calendar date (ET), ISO date
    filing_date: str                # ISO date — recorded for audit, NEVER the event key
    shares: float
    price: float
    value: float
    remaining_shares: float | None
    direct_indirect: str | None
    is_10b5_1: bool
    footnotes: str
    source: str = "edgartools:form4:non_derivative:P"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_identity() -> None:
    """Call ``edgar.set_identity`` once per process from env var or default."""
    global _identity_set
    if _identity_set:
        return
    from edgar import set_identity

    identity = os.environ.get(EDGAR_IDENTITY_ENV) or DEFAULT_IDENTITY
    set_identity(identity)
    _identity_set = True


# ---------------------------------------------------------------------------
# coercion helpers — edgartools hands back numpy scalars / datetimes / Nones
# ---------------------------------------------------------------------------


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _iso_date(v: Any) -> str | None:
    """Coerce a date-ish value (date, datetime, or 'YYYY-MM-DD' string) to ISO date."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    return s or None


def _iso_dt(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    s = str(v).strip()
    return s or None


def _event_date_from_acceptance(acceptance_dt: Any) -> str | None:
    """Derive the event calendar date (ET) from the acceptance timestamp.

    SEC acceptance timestamps are stamped in US Eastern. If the value is a naive
    datetime we treat it as ET (edgartools' convention). The event date is the
    calendar date of acceptance; downstream replay enters the *next* bar, which
    is look-ahead-safe regardless of intraday vs after-hours acceptance.
    """
    if acceptance_dt is None:
        return None
    if isinstance(acceptance_dt, datetime):
        dt = acceptance_dt
        if dt.tzinfo is not None:
            dt = dt.astimezone(ET)
        return dt.date().isoformat()
    # string fallback
    s = str(acceptance_dt).strip()
    if not s:
        return None
    # 'YYYY-MM-DD HH:MM:SS' or ISO
    return s.split("T")[0].split(" ")[0]


def _is_10b5_1(footnotes: str) -> bool:
    f = (footnotes or "").lower()
    return "10b5-1" in f or "10b5 1" in f


def _role_of(reporting_owners: Any) -> dict[str, Any]:
    """Pull role flags + title from the (primary) reporting owner.

    A Form 4 can name multiple reporting owners; we attribute the transaction to
    the first owner (the near-universal case is a single owner). Returns a dict
    with the role flags, name, cik, title, and a joined name string for audit.
    """
    out = {
        "insider_name": "",
        "insider_cik": "",
        "position": None,
        "is_director": False,
        "is_officer": False,
        "is_ten_pct_owner": False,
        "is_other": False,
        "officer_title": None,
    }
    if not reporting_owners:
        return out
    try:
        owners = list(reporting_owners)
    except TypeError:
        owners = [reporting_owners]
    if not owners:
        return out
    ro = owners[0]
    out["insider_name"] = str(getattr(ro, "name", "") or "")
    out["insider_cik"] = str(getattr(ro, "cik", "") or "")
    out["position"] = getattr(ro, "position", None) or getattr(ro, "officer_title", None)
    out["is_director"] = bool(getattr(ro, "is_director", False))
    out["is_officer"] = bool(getattr(ro, "is_officer", False))
    out["is_ten_pct_owner"] = bool(getattr(ro, "is_ten_pct_owner", False))
    out["is_other"] = bool(getattr(ro, "is_other", False))
    out["officer_title"] = getattr(ro, "officer_title", None)
    return out


# ---------------------------------------------------------------------------
# parse one filing
# ---------------------------------------------------------------------------


def parse_filing(filing: Any) -> list[InsiderPurchase]:
    """Parse one Form 4 :class:`edgar.Filing` into its open-market purchases.

    Returns ``[]`` for filings with no qualifying purchase (the common case) or
    that fail to parse. Never raises on a single bad filing — ingest of a day
    must not be aborted by one malformed document.

    The filing object must expose ``accession_no``/``accession_number``,
    ``filing_date``, ``header.acceptance_datetime``, and ``obj()`` returning a
    Form4-like with ``issuer``, ``reporting_owners``, and
    ``non_derivative_table.transactions``.
    """
    try:
        accession = str(
            getattr(filing, "accession_no", None)
            or getattr(filing, "accession_number", "")
        )
        filing_date = _iso_date(getattr(filing, "filing_date", None))

        # acceptanceDateTime — THE event key. A missing header is disqualifying:
        # without it we cannot place the event on the timeline safely.
        header = getattr(filing, "header", None)
        acceptance_raw = getattr(header, "acceptance_datetime", None) if header else None
        acceptance_iso = _iso_dt(acceptance_raw)
        event_date = _event_date_from_acceptance(acceptance_raw)
        if acceptance_iso is None or event_date is None:
            return []

        obj = filing.obj()
    except Exception:  # noqa: BLE001 — one bad filing must not kill the batch
        return []

    issuer = getattr(obj, "issuer", None)
    issuer_cik = str(getattr(issuer, "cik", "") or "")
    issuer_name = str(getattr(issuer, "name", "") or "")
    ticker = getattr(issuer, "ticker", None)
    if ticker is not None:
        ticker = str(ticker).strip().upper() or None

    role = _role_of(getattr(obj, "reporting_owners", None))

    ndt = getattr(obj, "non_derivative_table", None)
    transactions = getattr(ndt, "transactions", None) if ndt is not None else None
    if not transactions:
        return []

    out: list[InsiderPurchase] = []
    for t in transactions:
        code = str(getattr(t, "transaction_code", "") or "").strip().upper()
        acq = str(getattr(t, "acquired_disposed", "") or "").strip().upper()
        if code != PURCHASE_CODE or acq != ACQUIRED:
            continue

        shares = _to_float(getattr(t, "shares", None))
        price = _to_float(getattr(t, "price", None))
        if shares is None or shares <= 0:
            continue
        # price can legitimately be 0 on some private placements; keep but value=0
        price = price if price is not None else 0.0
        value = round(shares * price, 2)
        footnotes = str(getattr(t, "footnotes", "") or "")

        out.append(InsiderPurchase(
            accession_number=accession,
            issuer_cik=issuer_cik,
            ticker=ticker,
            issuer_name=issuer_name,
            insider_name=role["insider_name"],
            insider_cik=role["insider_cik"],
            position=role["position"],
            is_director=role["is_director"],
            is_officer=role["is_officer"],
            is_ten_pct_owner=role["is_ten_pct_owner"],
            is_other=role["is_other"],
            officer_title=role["officer_title"],
            transaction_date=_iso_date(getattr(t, "date", None)) or event_date,
            acceptance_datetime=acceptance_iso,
            event_date=event_date,
            filing_date=filing_date or event_date,
            shares=shares,
            price=price,
            value=value,
            remaining_shares=_to_float(getattr(t, "remaining", None)),
            direct_indirect=(str(getattr(t, "direct_indirect", "") or "").strip() or None),
            is_10b5_1=_is_10b5_1(footnotes),
            footnotes=footnotes,
        ))
    return out


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def _cache_path(date_str: str, cache_dir: Path) -> Path:
    return cache_dir / f"{date_str}.json"


def _today_et() -> date:
    return datetime.now(ET).date()


def _is_past_day(date_str: str) -> bool:
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return False
    return d < _today_et()


def _read_day_cache(date_str: str, cache_dir: Path) -> list[InsiderPurchase] | None:
    p = _cache_path(date_str, cache_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    cached_at = data.get("_cached_at_epoch")
    if not isinstance(cached_at, (int, float)):
        return None
    # Past days are immutable → cache never expires. Today → short TTL.
    if not _is_past_day(date_str):
        if (time.time() - cached_at) > TODAY_CACHE_TTL_SECONDS:
            return None
    payload = data.get("payload")
    if not isinstance(payload, list):
        return None
    try:
        return [InsiderPurchase(**row) for row in payload]
    except TypeError:
        return None


def _write_day_cache(date_str: str, rows: list[InsiderPurchase], cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = _cache_path(date_str, cache_dir)
    p.write_text(json.dumps({
        "_cached_at_epoch": time.time(),
        "_cached_at_iso": _utc_now_iso(),
        "date": date_str,
        "count": len(rows),
        "payload": [r.to_dict() for r in rows],
    }, indent=2))


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


def _default_day_filings_factory(date_str: str) -> Iterable[Any]:
    """Real EDGAR call: all Form 4 (non-amendment) filings accepted on a day."""
    from edgar import get_insider_transaction_filings

    fs = get_insider_transaction_filings(form="4", filing_date=date_str, amendments=False)
    return fs if fs is not None else []


def _default_current_filings_factory() -> Iterable[Any]:
    """Real EDGAR call: the live 'getcurrent' Form 4 feed (owner-only)."""
    from edgar import get_current_filings

    fs = get_current_filings(form="4", owner="only", page_size=100)
    return fs if fs is not None else []


def ingest_day(
    date_str: str,
    *,
    cache_dir: Path | None = None,
    use_cache: bool = True,
    limit: int | None = None,
    _filings_factory: Callable[[str], Iterable[Any]] | None = None,
) -> list[InsiderPurchase]:
    """Return every open-market insider purchase accepted on ``date_str``.

    Args:
        date_str: ISO date ``YYYY-MM-DD`` (interpreted as the SEC filing day).
        cache_dir: override cache location.
        use_cache: serve a fresh cache hit without hitting EDGAR (and skip the
            write when ``False``). ``limit`` runs are never cached, since a
            truncated result must not masquerade as the full day.
        limit: parse at most this many filings (for smoke tests / sampling).
        _filings_factory: test seam — ``date_str -> iterable of Filing-like``.

    Note: there is no index-level purchase filter, so a full day parses every
    Form 4 (network-heavy). The per-day cache makes that a one-time cost.
    """
    cache_dir = cache_dir or CACHE_DIR_DEFAULT
    cacheable = use_cache and limit is None

    if cacheable:
        cached = _read_day_cache(date_str, cache_dir)
        if cached is not None:
            return cached

    factory = _filings_factory or _default_day_filings_factory
    if _filings_factory is None:
        _ensure_identity()

    filings = factory(date_str)
    rows: list[InsiderPurchase] = []
    n = 0
    for f in filings:
        if limit is not None and n >= limit:
            break
        n += 1
        rows.extend(parse_filing(f))

    if cacheable:
        _write_day_cache(date_str, rows, cache_dir)
    return rows


def ingest_current(
    *,
    limit: int | None = None,
    _filings_factory: Callable[[], Iterable[Any]] | None = None,
) -> list[InsiderPurchase]:
    """Return open-market purchases from the live SEC 'getcurrent' Form 4 feed.

    Not cached — this is the live edge of the timeline and is meant to be polled.
    """
    factory = _filings_factory or _default_current_filings_factory
    if _filings_factory is None:
        _ensure_identity()

    filings = factory()
    rows: list[InsiderPurchase] = []
    n = 0
    for f in filings:
        if limit is not None and n >= limit:
            break
        n += 1
        rows.extend(parse_filing(f))
    return rows


def compute(
    date_str: str,
    *,
    cache_dir: Path | None = None,
    use_cache: bool = True,
    limit: int | None = None,
    _filings_factory: Callable[[str], Iterable[Any]] | None = None,
) -> TraceEntry:
    """Library entry — returns a :class:`TraceEntry` for a day's purchases."""
    rows = ingest_day(
        date_str,
        cache_dir=cache_dir,
        use_cache=use_cache,
        limit=limit,
        _filings_factory=_filings_factory,
    )
    return TraceEntry(
        tool=TOOL,
        inputs={"date": date_str, "limit": limit},
        output={
            "date": date_str,
            "n_purchases": len(rows),
            "purchases": [r.to_dict() for r in rows],
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.fundamentals.form4_insider_transactions",
        description="Ingest open-market insider purchases (Form 4, code P) via SEC EDGAR.",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--date", help="ISO filing day YYYY-MM-DD")
    g.add_argument("--current", action="store_true", help="live getcurrent feed")
    p.add_argument("--limit", type=int, default=None, help="parse at most N filings")
    p.add_argument("--no-cache", action="store_true", dest="no_cache")
    args = p.parse_args()

    if args.current:
        rows = ingest_current(limit=args.limit)
        emit(TraceEntry(
            tool=TOOL,
            inputs={"feed": "getcurrent", "limit": args.limit},
            output={"n_purchases": len(rows), "purchases": [r.to_dict() for r in rows]},
        ))
    else:
        emit(compute(args.date, use_cache=not args.no_cache, limit=args.limit))


if __name__ == "__main__":
    main()
