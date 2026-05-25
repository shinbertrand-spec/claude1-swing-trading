"""TTM EPS lookup via SEC EDGAR (edgartools).

Pulls trailing-twelve-month diluted EPS for a US-listed ticker by combining:

* ``Company.get_ttm_net_income()`` — sum of net income across the last four
  reported quarters (XBRL concept ``us-gaap:NetIncomeLoss``)
* ``Company.get_financials().get_shares_outstanding_diluted()`` — diluted
  shares from the most-recently-filed 10-K or 10-Q

Disk-cached for 24 hours per ticker — EPS doesn't change intra-day, and SEC
EDGAR HTTP calls are rate-limited.

Identity: SEC requires every API caller to send an identity header. Set
``EDGAR_IDENTITY`` env var (e.g. ``"Bertrand Shin shinbertrand@gmail.com"``).
If unset, falls back to :data:`DEFAULT_IDENTITY`.

CLI::

    uv run python -m tools.fundamentals.edgar_eps --ticker AAPL
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..cli import emit
from ..contract import TraceEntry

TOOL = "tools/fundamentals/edgar_eps.py"

EDGAR_IDENTITY_ENV = "EDGAR_IDENTITY"
DEFAULT_IDENTITY = "Bertrand Shin shinbertrand@gmail.com"

CACHE_DIR_DEFAULT = Path(__file__).resolve().parents[2] / "tools" / "cache" / "edgar_eps"
CACHE_TTL_SECONDS = 86400  # 24h

_identity_set = False


class EdgarEPSError(Exception):
    """Raised when EDGAR returns no usable EPS data for a ticker."""


@dataclass
class EPSResult:
    ticker: str
    ttm_eps: float
    ttm_net_income: float
    diluted_shares: float
    period_label: str
    fetched_at: str
    source: str

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


def _cache_path(ticker: str, cache_dir: Path) -> Path:
    return cache_dir / f"{ticker.upper()}.json"


def _read_cache(ticker: str, cache_dir: Path) -> EPSResult | None:
    p = _cache_path(ticker, cache_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    cached_at = data.get("_cached_at_epoch")
    if not isinstance(cached_at, (int, float)):
        return None
    if (time.time() - cached_at) > CACHE_TTL_SECONDS:
        return None
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return None
    try:
        return EPSResult(**payload)
    except TypeError:
        return None


def _write_cache(ticker: str, result: EPSResult, cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = _cache_path(ticker, cache_dir)
    p.write_text(json.dumps({
        "_cached_at_epoch": time.time(),
        "_cached_at_iso": _utc_now_iso(),
        "payload": result.to_dict(),
    }, indent=2))


def fetch_ttm_eps(
    ticker: str,
    *,
    cache_dir: Path | None = None,
    use_cache: bool = True,
    _company_factory=None,
) -> EPSResult:
    """Return TTM diluted EPS for ``ticker``.

    Args:
        ticker: US-listed equity (case insensitive).
        cache_dir: override cache location (default
            ``tools/cache/edgar_eps/``).
        use_cache: when True (default), serve fresh cache hits without
            hitting EDGAR.
        _company_factory: test seam — function taking a ticker string and
            returning a Company-like object. Defaults to
            ``edgar.Company``.

    Raises:
        EdgarEPSError: when EDGAR returns no usable data (unknown ticker,
            missing TTM net income, missing diluted shares, or zero
            shares).
    """
    ticker = ticker.upper().strip()
    cache_dir = cache_dir or CACHE_DIR_DEFAULT

    if use_cache:
        cached = _read_cache(ticker, cache_dir)
        if cached is not None:
            return cached

    _ensure_identity()

    if _company_factory is None:
        from edgar import Company as _Company
        _company_factory = _Company

    try:
        co = _company_factory(ticker)
    except Exception as exc:  # noqa: BLE001 — edgartools throws various types
        raise EdgarEPSError(f"EDGAR company lookup failed for {ticker}: {exc}") from exc

    # TTM net income
    try:
        ttm = co.get_ttm_net_income()
    except Exception as exc:  # noqa: BLE001
        raise EdgarEPSError(f"EDGAR TTM net income lookup failed for {ticker}: {exc}") from exc
    if ttm is None:
        raise EdgarEPSError(f"EDGAR returned no TTM net income for {ticker}")
    ttm_value = float(getattr(ttm, "value", ttm))
    period_label = ", ".join(str(p) for p in getattr(ttm, "periods", [])) or "TTM"

    # Diluted shares
    try:
        fin = co.get_financials()
    except Exception as exc:  # noqa: BLE001
        raise EdgarEPSError(f"EDGAR financials lookup failed for {ticker}: {exc}") from exc
    if fin is None:
        raise EdgarEPSError(f"EDGAR returned no financials for {ticker}")
    try:
        diluted = fin.get_shares_outstanding_diluted()
    except Exception as exc:  # noqa: BLE001
        raise EdgarEPSError(f"EDGAR diluted-shares lookup failed for {ticker}: {exc}") from exc
    if diluted is None or float(diluted) <= 0:
        raise EdgarEPSError(f"EDGAR returned non-positive diluted shares for {ticker}: {diluted!r}")
    diluted_value = float(diluted)

    ttm_eps = ttm_value / diluted_value

    result = EPSResult(
        ticker=ticker,
        ttm_eps=ttm_eps,
        ttm_net_income=ttm_value,
        diluted_shares=diluted_value,
        period_label=period_label,
        fetched_at=_utc_now_iso(),
        source="edgartools:us-gaap:NetIncomeLoss/diluted_shares",
    )

    if use_cache:
        _write_cache(ticker, result, cache_dir)

    return result


def compute(
    ticker: str,
    *,
    cache_dir: Path | None = None,
    use_cache: bool = True,
    _company_factory=None,
) -> TraceEntry:
    """Library entry — returns a :class:`TraceEntry` ready to append to a ledger."""
    res = fetch_ttm_eps(
        ticker,
        cache_dir=cache_dir,
        use_cache=use_cache,
        _company_factory=_company_factory,
    )
    return TraceEntry(
        tool=TOOL,
        inputs={"ticker": ticker.upper()},
        output=res.to_dict(),
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.fundamentals.edgar_eps",
        description="Pull TTM diluted EPS via SEC EDGAR (edgartools).",
    )
    p.add_argument("--ticker", required=True)
    p.add_argument("--no-cache", action="store_true", dest="no_cache")
    args = p.parse_args()
    emit(compute(args.ticker, use_cache=not args.no_cache))


if __name__ == "__main__":
    main()
