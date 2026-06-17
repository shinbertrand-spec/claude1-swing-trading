"""Bulk SEC Form 345 insider-transactions loader (fast backfill path).

The per-filing EDGAR ingest (:mod:`tools.fundamentals.form4_insider_transactions`)
is correct but must parse every Form 4 filed each day (~2,000) to find the
purchases — days of wall-clock for a multi-year backfill. The SEC also publishes
**quarterly bulk datasets** ("Insider Transactions Data Sets", Form 345) — one
~14 MB zip per quarter with ALL Form 3/4/5 transactions already parsed into TSV
tables. This loader turns a multi-year backfill into minutes of downloads.

Roles split: the per-filing path stays for the LIVE getcurrent feed (it carries
the full ``acceptanceDateTime``); this bulk path is for historical BACKFILL.
The bulk ``SUBMISSION`` table has ``FILING_DATE`` at day granularity (not the
sub-daily acceptance timestamp). That is anti-look-ahead-safe for the daily
next-bar-open backtest: filing_date is the public date and entry is always the
*next* bar, so no future information leaks. The sub-daily timestamp only mattered
for intraday live decisions.

Bonus: the bulk ``AFF10B5ONE`` column is the actual Form-4 10b5-1 checkbox —
more reliable than the per-filing footnote heuristic.

Emits the same :class:`InsiderPurchase` rows as the per-filing path (code ``P``,
acquired side), so it is a drop-in source for
:func:`tools.fundamentals.insider_events.build_events`.

CLI::

    uv run python -m tools.fundamentals.form345_bulk --year 2024 --quarter 1 --limit 10
"""
from __future__ import annotations

import argparse
import csv
import io
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from .form4_insider_transactions import InsiderPurchase

CACHE_DIR_DEFAULT = Path(__file__).resolve().parents[2] / "tools" / "cache" / "form345"
BASE_URL = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets"
DEFAULT_IDENTITY = "Bertrand Shin shinbertrand@gmail.com"

PURCHASE_CODE = "P"
ACQUIRED = "A"
SOURCE = "sec-bulk:form345:NONDERIV:P"


class Form345BulkError(Exception):
    """Raised when a bulk quarter cannot be fetched or parsed."""


# ---------------------------------------------------------------------------
# field coercion
# ---------------------------------------------------------------------------


def _parse_sec_date(s: str) -> Optional[str]:
    """Parse SEC bulk 'DD-MON-YYYY' (e.g. '28-FEB-2024') to ISO date string."""
    if not s or not s.strip():
        return None
    try:
        return datetime.strptime(s.strip().title(), "%d-%b-%Y").date().isoformat()
    except ValueError:
        # some rows are already ISO
        try:
            return date.fromisoformat(s.strip()[:10]).isoformat()
        except ValueError:
            return None


def _to_float(s: Any) -> Optional[float]:
    if s is None or str(s).strip() == "":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _roles(relationship: str) -> dict[str, bool]:
    parts = {p.strip().lower() for p in (relationship or "").split(",")}
    return {
        "is_director": "director" in parts,
        "is_officer": "officer" in parts,
        "is_ten_pct_owner": "tenpercentowner" in parts,
        "is_other": "other" in parts,
    }


# ---------------------------------------------------------------------------
# download + cache
# ---------------------------------------------------------------------------


def _quarter_url(year: int, quarter: int) -> str:
    return f"{BASE_URL}/{year}q{quarter}_form345.zip"


def download_quarter(year: int, quarter: int, *, cache_dir: Path = CACHE_DIR_DEFAULT,
                     force: bool = False) -> Path:
    """Download (and disk-cache) one quarterly bulk zip. Returns the cached path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{year}q{quarter}_form345.zip"
    if path.is_file() and not force and path.stat().st_size > 0:
        return path
    import urllib.request
    req = urllib.request.Request(_quarter_url(year, quarter),
                                 headers={"User-Agent": DEFAULT_IDENTITY})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
    except Exception as exc:  # noqa: BLE001
        raise Form345BulkError(f"download failed for {year}Q{quarter}: {exc}") from exc
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def _read_tsv(zf: zipfile.ZipFile, name: str) -> Iterable[dict[str, str]]:
    with zf.open(name) as f:
        reader = csv.DictReader(io.TextIOWrapper(f, "utf-8", "replace"), delimiter="\t")
        for row in reader:
            yield row


def parse_zip(zip_path: Path, *, forms: tuple[str, ...] = ("4",),
              limit: Optional[int] = None) -> list[InsiderPurchase]:
    """Parse a bulk quarter zip into open-market purchase rows (code P / acquired A).

    Joins NONDERIV_TRANS (the P transactions) ⨝ SUBMISSION (issuer + filing date
    + 10b5-1 flag) ⨝ REPORTINGOWNER (first owner's roles/name). ``forms``
    restricts DOCUMENT_TYPE (default Form 4 only, excluding 4/A amendments).
    """
    try:
        zf = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise Form345BulkError(f"bad zip {zip_path}: {exc}") from exc

    forms_set = set(forms)
    submissions: dict[str, dict[str, str]] = {}
    for row in _read_tsv(zf, "SUBMISSION.tsv"):
        if row.get("DOCUMENT_TYPE") in forms_set:
            submissions[row["ACCESSION_NUMBER"]] = row

    owners: dict[str, dict[str, str]] = {}
    for row in _read_tsv(zf, "REPORTINGOWNER.tsv"):
        acc = row["ACCESSION_NUMBER"]
        if acc in submissions and acc not in owners:   # first owner per filing
            owners[acc] = row

    out: list[InsiderPurchase] = []
    for row in _read_tsv(zf, "NONDERIV_TRANS.tsv"):
        if (row.get("TRANS_CODE", "").strip().upper() != PURCHASE_CODE
                or row.get("TRANS_ACQUIRED_DISP_CD", "").strip().upper() != ACQUIRED):
            continue
        acc = row["ACCESSION_NUMBER"]
        sub = submissions.get(acc)
        if sub is None:
            continue
        shares = _to_float(row.get("TRANS_SHARES"))
        if shares is None or shares <= 0:
            continue
        price = _to_float(row.get("TRANS_PRICEPERSHARE")) or 0.0

        filing_iso = _parse_sec_date(sub.get("FILING_DATE", ""))
        if filing_iso is None:
            continue
        owner = owners.get(acc, {})
        role = _roles(owner.get("RPTOWNER_RELATIONSHIP", ""))
        ticker = (sub.get("ISSUERTRADINGSYMBOL") or "").strip().upper()
        ticker = ticker if ticker and ticker not in ("NONE", "N/A") else None
        title = owner.get("RPTOWNER_TITLE") or None

        out.append(InsiderPurchase(
            accession_number=acc,
            issuer_cik=str(sub.get("ISSUERCIK", "") or ""),
            ticker=ticker,
            issuer_name=str(sub.get("ISSUERNAME", "") or ""),
            insider_name=str(owner.get("RPTOWNERNAME", "") or ""),
            insider_cik=str(owner.get("RPTOWNERCIK", "") or ""),
            position=title,
            is_director=role["is_director"],
            is_officer=role["is_officer"],
            is_ten_pct_owner=role["is_ten_pct_owner"],
            is_other=role["is_other"],
            officer_title=title,
            transaction_date=_parse_sec_date(row.get("TRANS_DATE", "")) or filing_iso,
            acceptance_datetime=filing_iso,   # day granularity (bulk has no time)
            event_date=filing_iso,
            filing_date=filing_iso,
            shares=shares,
            price=price,
            value=round(shares * price, 2),
            remaining_shares=_to_float(row.get("SHRS_OWND_FOLWNG_TRANS")),
            direct_indirect=(row.get("DIRECT_INDIRECT_OWNERSHIP") or "").strip() or None,
            is_10b5_1=str(sub.get("AFF10B5ONE", "")).strip() == "1",
            footnotes="",
            source=SOURCE,
        ))
        if limit is not None and len(out) >= limit:
            break
    return out


def load_quarter_purchases(year: int, quarter: int, *, cache_dir: Path = CACHE_DIR_DEFAULT,
                           forms: tuple[str, ...] = ("4",),
                           limit: Optional[int] = None) -> list[InsiderPurchase]:
    """Download (cached) + parse one quarter's open-market purchases."""
    path = download_quarter(year, quarter, cache_dir=cache_dir)
    return parse_zip(path, forms=forms, limit=limit)


def _quarters_in_range(start: date, end: date) -> list[tuple[int, int]]:
    qs: list[tuple[int, int]] = []
    y, q = start.year, (start.month - 1) // 3 + 1
    ey, eq = end.year, (end.month - 1) // 3 + 1
    while (y, q) <= (ey, eq):
        qs.append((y, q))
        q += 1
        if q > 4:
            q = 1
            y += 1
    return qs


def ingest_range(start: date, end: date, *, cache_dir: Path = CACHE_DIR_DEFAULT,
                 forms: tuple[str, ...] = ("4",)) -> list[InsiderPurchase]:
    """All open-market purchases with filing_date in [start, end] across quarters."""
    out: list[InsiderPurchase] = []
    for (y, q) in _quarters_in_range(start, end):
        for p in load_quarter_purchases(y, q, cache_dir=cache_dir, forms=forms):
            try:
                fd = date.fromisoformat(p.filing_date)
            except (ValueError, TypeError):
                continue
            if start <= fd <= end:
                out.append(p)
    out.sort(key=lambda p: (p.filing_date, p.ticker or ""))
    return out


def main() -> None:
    p = argparse.ArgumentParser(prog="tools.fundamentals.form345_bulk")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--quarter", type=int, required=True, choices=[1, 2, 3, 4])
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()
    rows = load_quarter_purchases(args.year, args.quarter, limit=args.limit)
    print(f"{len(rows)} purchases in {args.year}Q{args.quarter}")
    for r in rows[:20]:
        print(f"  {r.filing_date} {r.ticker or r.issuer_name[:16]:8} "
              f"{r.insider_name[:22]:22} {r.shares:>12.0f} @ {r.price:>9.2f} "
              f"dir={r.is_director} off={r.is_officer} 10pct={r.is_ten_pct_owner} 10b5={r.is_10b5_1}")


if __name__ == "__main__":
    main()
