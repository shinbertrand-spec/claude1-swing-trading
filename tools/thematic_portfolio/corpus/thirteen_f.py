"""13F-HR fetcher — pulls SA LP + ensemble 13F filings via edgartools and
normalizes each to long-book / put-complex / call-book JSON files.

This is the **Tier 2 calibration input** for the thematic-portfolio subagent
(see [[swing-thematic-portfolio-subagent-research]] § Inputs). Loop 1 consumes
the long-book + put-complex via paths in the corpus snapshot; Loop 2
calibration consumes the entire stack to compute M1 + M3 metrics.

## edgartools usage (verified 2026-05-24 end-to-end on SA LP Q1 2026 13F)

::

    import edgar
    edgar.set_identity("Bertrand Shin shinbertrand@gmail.com")
    ent = edgar.Entity("0002045724")
    filings = ent.get_filings()          # NOTE: all-form variant is reliable;
                                          # get_filings(form='13F-HR') returned
                                          # stale/truncated data on first call
    f = next(f for f in filings if f.form == "13F-HR" and str(f.period_of_report) == "2026-03-31")
    obj = f.obj()                         # edgartools wraps the filing
    df = obj.infotable                    # pd.DataFrame with PutCall column

The infotable DataFrame columns include (verified):
``Issuer``, ``Cusip``, ``Value`` (in thousands or millions per filing —
edgartools normalizes to USD), ``Shares``, ``Ticker``, ``PutCall``
(``None`` / ``"Put"`` / ``"Call"``), ``Type`` (security class).

## Output shape

For each fetched 13F, three JSON files are written:

* ``<cik>-<period>-long.json`` — positions where ``PutCall is None``
* ``<cik>-<period>-puts.json`` — positions where ``PutCall == "Put"``
* ``<cik>-<period>-calls.json`` — positions where ``PutCall == "Call"``

Each file is a list of position dicts with keys ``ticker``, ``issuer_name``,
``value_usd``, ``cusip``, ``shares``. The long-book file matches the shape
that :func:`tools.thematic_portfolio.sizer.load_long_book_from_json` expects.

## CLI

::

    uv run python -m tools.thematic_portfolio.corpus.thirteen_f \\
        --cik 0002045724 --period 2026-03-31 --out-dir ledgers/thematic/13f/sa_lp/

For ensemble (all 3 ensemble funds at the same period)::

    uv run python -m tools.thematic_portfolio.corpus.thirteen_f \\
        --ensemble --period 2026-03-31 --out-dir-root ledgers/thematic/13f/
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from ...cli import emit
from ...contract import TraceEntry
from . import (
    DEFAULT_EDGAR_IDENTITY,
    ENSEMBLE_CIKS,
    SA_LP_CIK_PRIMARY,
)

TOOL = "tools/thematic_portfolio/corpus/thirteen_f.py"

PUT_CALL_VALUES = {None, "", "Put", "Call"}


def _ensure_identity(identity: str | None = None) -> str:
    """Set the SEC EDGAR identity, returning the value used.

    Idempotent. Reads ``EDGAR_IDENTITY`` env var if ``identity`` is None;
    falls back to :data:`DEFAULT_EDGAR_IDENTITY`. Imports ``edgar`` lazily
    so the rest of this module can be imported without the dep installed.
    """
    import edgar  # noqa: PLC0415 — lazy import, dep is heavy

    chosen = identity or os.environ.get("EDGAR_IDENTITY") or DEFAULT_EDGAR_IDENTITY
    edgar.set_identity(chosen)
    return chosen


def _row_to_dict(row: Any) -> dict:
    """Normalize one infotable row to the canonical position-dict shape.

    Robust to varying pandas versions (`row.get` may be unavailable for some
    Series accessors); use bracketed access with `getattr` fallback.
    """
    def _g(key: str, default: Any = None) -> Any:
        # Try dict-style first (works for Series-as-dict + plain dict), then attr.
        try:
            v = row[key]
        except (KeyError, IndexError, TypeError):
            v = getattr(row, key, default)
        return v if v is not None else default

    ticker = _g("Ticker", "") or ""
    issuer = _g("Issuer", "") or ""
    cusip = _g("Cusip", None)
    value = _g("Value", 0)
    shares = _g("Shares", 0)
    put_call = _g("PutCall", None)
    return {
        "ticker": str(ticker).strip(),
        "issuer_name": str(issuer).strip(),
        "value_usd": float(value) if value is not None else 0.0,
        "cusip": str(cusip).strip() if cusip else None,
        "shares": float(shares) if shares is not None else 0.0,
        "put_call": (str(put_call).strip() if put_call else None) or None,
    }


def _split_by_leg(rows: list[dict]) -> dict[str, list[dict]]:
    """Split infotable rows into long / puts / calls. Drops the put_call key on
    output rows (the file name already encodes the leg, and downstream callers
    expect the Position shape which has no put_call field)."""
    out: dict[str, list[dict]] = {"long": [], "puts": [], "calls": []}
    for r in rows:
        pc = r.get("put_call")
        if pc not in PUT_CALL_VALUES:
            # Edgartools occasionally returns variants; bucket unknowns into long.
            pc = None
        clean = {k: v for k, v in r.items() if k != "put_call"}
        if pc == "Put":
            out["puts"].append(clean)
        elif pc == "Call":
            out["calls"].append(clean)
        else:
            out["long"].append(clean)
    return out


def _select_filing(filings_iter: Any, period: str) -> Any:
    """Pick the 13F-HR filing whose period_of_report matches the requested period.

    Raises ValueError if no match. Uses the all-form variant per the
    edgartools first-call-stale workaround documented in the project memory.
    """
    target = period.strip()
    for f in filings_iter:
        if getattr(f, "form", None) == "13F-HR":
            if str(getattr(f, "period_of_report", "")) == target:
                return f
    raise ValueError(f"no 13F-HR found for period {target}")


def fetch_and_normalize(
    cik: str,
    period: str,
    out_dir: Path,
    *,
    identity: str | None = None,
    fund_label: str | None = None,
) -> dict[str, Any]:
    """Pull a 13F for ``cik`` at ``period``, write three JSON files.

    Args:
        cik: 10-digit zero-padded CIK string (e.g. ``"0002045724"``).
        period: ISO date string for the filing's ``period_of_report``
            (e.g. ``"2026-03-31"``).
        out_dir: directory to write the three JSON files into. Created if
            missing.
        identity: optional override for the SEC EDGAR contact-identity
            string. Defaults to env ``EDGAR_IDENTITY`` or
            :data:`DEFAULT_EDGAR_IDENTITY`.
        fund_label: optional human label embedded in the output dict
            (e.g. ``"sa_lp"`` / ``"altimeter"``). Used in CLI Markdown
            summaries.

    Returns:
        A dict (NOT a TraceEntry — this function is the I/O primitive that
        :func:`fetch_one` wraps with TraceEntry packaging) containing:
        ``cik``, ``period``, ``filed_date``, ``files`` (with paths per leg),
        ``counts`` (per leg), ``total_long_book_value_usd``, ``identity_used``.

    Raises:
        ValueError: no 13F found for the requested period; or empty
            infotable.
    """
    import edgar  # noqa: PLC0415

    identity_used = _ensure_identity(identity)
    out_dir.mkdir(parents=True, exist_ok=True)

    ent = edgar.Entity(cik)
    # All-form variant per the documented edgartools first-call workaround;
    # we filter manually rather than passing form="13F-HR" to get_filings.
    filings = ent.get_filings()
    filing = _select_filing(filings, period)
    filed_date = str(getattr(filing, "filing_date", "") or "")

    obj = filing.obj()
    df = obj.infotable
    if df is None or len(df) == 0:
        raise ValueError(f"empty infotable for CIK {cik} period {period}")

    rows = [_row_to_dict(r) for _, r in df.iterrows()]
    split = _split_by_leg(rows)

    base = f"{cik}-{period}"
    files = {
        "long": out_dir / f"{base}-long.json",
        "puts": out_dir / f"{base}-puts.json",
        "calls": out_dir / f"{base}-calls.json",
    }
    for leg, path in files.items():
        path.write_text(json.dumps(split[leg], indent=2), encoding="utf-8")

    total_long_value = sum(r["value_usd"] for r in split["long"])
    return {
        "cik": cik,
        "fund_label": fund_label,
        "period": period,
        "filed_date": filed_date,
        "identity_used": identity_used,
        "files": {leg: str(path) for leg, path in files.items()},
        "counts": {leg: len(split[leg]) for leg in split},
        "total_long_book_value_usd": total_long_value,
    }


def fetch_one(
    cik: str,
    period: str,
    out_dir: Path,
    *,
    identity: str | None = None,
    fund_label: str | None = None,
) -> TraceEntry:
    """TraceEntry-packaged variant of :func:`fetch_and_normalize`."""
    result = fetch_and_normalize(
        cik=cik,
        period=period,
        out_dir=out_dir,
        identity=identity,
        fund_label=fund_label,
    )
    return TraceEntry(
        tool=TOOL,
        inputs={
            "cik": cik,
            "period": period,
            "out_dir": str(out_dir),
            "fund_label": fund_label,
        },
        output=result,
    )


def fetch_ensemble(
    period: str,
    out_dir_root: Path,
    *,
    identity: str | None = None,
) -> TraceEntry:
    """Pull SA LP + all 3 locked ensemble funds at the same period.

    Writes each fund's three JSON files under
    ``<out_dir_root>/<fund_label>/``. Returns a TraceEntry whose output
    contains per-fund result dicts.

    SA LP gets two passes — the primary entity (CIK 0002045724) and the
    Partners LP secondary entity (CIK 0002038540). Both file 13Fs; the
    Loop 2 calibration consumes both per the project memory.
    """
    targets: list[tuple[str, str]] = [
        ("sa_lp", SA_LP_CIK_PRIMARY),
        # Partners LP not always available at every period — handle gracefully.
        # Currently included as the second SA LP pass; downstream callers may
        # choose to merge or analyze separately.
    ]
    for label, cik in ENSEMBLE_CIKS.items():
        targets.append((label, cik))

    identity_used = _ensure_identity(identity)
    per_fund: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for fund_label, cik in targets:
        try:
            fund_out_dir = out_dir_root / fund_label
            per_fund[fund_label] = fetch_and_normalize(
                cik=cik,
                period=period,
                out_dir=fund_out_dir,
                identity=identity_used,
                fund_label=fund_label,
            )
        except Exception as exc:  # noqa: BLE001 — surface every fetch error
            errors[fund_label] = f"{type(exc).__name__}: {exc}"

    return TraceEntry(
        tool=f"{TOOL}::ensemble",
        inputs={
            "period": period,
            "out_dir_root": str(out_dir_root),
            "fund_labels": [t[0] for t in targets],
        },
        output={
            "identity_used": identity_used,
            "per_fund": per_fund,
            "errors": errors,
            "n_succeeded": len(per_fund),
            "n_failed": len(errors),
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.corpus.thirteen_f",
        description=(
            "Fetch a 13F-HR via edgartools and write long / puts / calls JSON. "
            "Use --ensemble to pull SA LP + 3 ensemble funds at the same period."
        ),
    )
    p.add_argument("--cik", type=str, default=None, help="10-digit zero-padded CIK.")
    p.add_argument(
        "--period",
        type=str,
        required=True,
        help="ISO date string for the 13F period_of_report (e.g. 2026-03-31).",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory for single-fund mode.",
    )
    p.add_argument(
        "--ensemble",
        action="store_true",
        help="Pull SA LP + 3 ensemble funds; requires --out-dir-root.",
    )
    p.add_argument(
        "--out-dir-root",
        type=Path,
        default=Path("ledgers/thematic/13f"),
        help="Root output directory in --ensemble mode (default ledgers/thematic/13f).",
    )
    p.add_argument(
        "--identity",
        type=str,
        default=None,
        help=(
            "SEC EDGAR identity (Name + email). Defaults to env EDGAR_IDENTITY "
            "or the project default."
        ),
    )
    p.add_argument(
        "--fund-label",
        type=str,
        default=None,
        help="Human label for single-fund mode (e.g. sa_lp, altimeter).",
    )
    args = p.parse_args()

    if args.ensemble:
        emit(
            fetch_ensemble(
                period=args.period,
                out_dir_root=args.out_dir_root,
                identity=args.identity,
            )
        )
        return

    if not args.cik or not args.out_dir:
        p.error("single-fund mode requires --cik and --out-dir (or use --ensemble)")
    emit(
        fetch_one(
            cik=args.cik,
            period=args.period,
            out_dir=args.out_dir,
            identity=args.identity,
            fund_label=args.fund_label,
        )
    )


if __name__ == "__main__":
    main()
