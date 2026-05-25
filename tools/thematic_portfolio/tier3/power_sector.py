"""Power-sector Tier 3 signal compiler.

Pulls a curated snapshot of AI-power-exposed equities (hyperscalers +
utilities with data-center load + power-infra equipment makers +
crypto-miner pivot plays) and writes a single JSON file Loop 1 cites
under ``tier3_signals.power_sector``.

V1 data source: ``yfinance`` (already installed). For each ticker we
pull:

* current ``price_usd`` (regularMarketPrice or info.previousClose)
* ``market_cap_usd``
* ``trailing_pe`` (TTM P/E)
* ``trailing_eps`` (TTM diluted EPS)
* ``next_earnings_date`` (when available — many tickers do not expose this)

Output JSON shape (locked in tests; v2 may add fields, never remove)::

    {
      "schema_version": "1.0",
      "fetched_at": "<ISO-8601 UTC>",
      "categories": {
        "hyperscaler": ["MSFT", "META", ...],
        "utility_data_center_exposed": ["CEG", "VST", ...],
        "power_infra_equipment": ["GEV", "BE", "PWR"],
        "miner_pivot": ["APLD", "IREN", ...]
      },
      "tickers": [
        {
          "ticker": "CEG",
          "category": "utility_data_center_exposed",
          "price_usd": 277.22,
          "market_cap_usd": 88_200_000_000.0,
          "trailing_pe": 22.5,
          "trailing_eps": 12.30,
          "next_earnings_date": "2026-07-30",
          "fetch_status": "ok"
        },
        ...
      ],
      "n_tickers_attempted": 19,
      "n_tickers_ok": 18,
      "errors": [{"ticker": "X", "reason": "..."}]
    }

Per-ticker failures NEVER crash the compiler — they land in ``errors``
with ``fetch_status: "error"`` and the ticker still appears in
``tickers[]`` so the LLM sees the gap explicitly rather than silently
missing the symbol.

CLI::

    uv run python -m tools.thematic_portfolio.tier3.power_sector \\
        --out ledgers/thematic/tier3/power_sector.json

Library::

    from tools.thematic_portfolio.tier3.power_sector import compose
    trace = compose(out_path=Path("..."))
    print(trace.output["n_tickers_ok"])
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ...cli import emit
from ...contract import TraceEntry

TOOL = "tools/thematic_portfolio/tier3/power_sector.py"
SCHEMA_VERSION = "1.0"

# Curated tickers. Categories map to the SA LP / Aschenbrenner thesis
# layers: hyperscalers = AI-compute demand; utilities = AI-power supply;
# power-infra = the bridge layer; miner-pivots = the SA LP-style plays
# that get capital because they sit at the demand/supply intersection.
#
# Discipline: these lists encode FUND IDENTITY (hyperscaler / utility),
# not fund-position pairs. Per session-2 design change #6, fund-position
# pairs are illustrative and must not be hardcoded — but tickers grouped
# by SECTOR ROLE are fund-identity-equivalent and stable across cycles.
POWER_TICKERS: dict[str, list[str]] = {
    "hyperscaler": ["MSFT", "META", "GOOGL", "AMZN", "ORCL"],
    "utility_data_center_exposed": [
        "CEG",   # Constellation Energy — Three Mile Island restart, MSFT PPA
        "VST",   # Vistra — Comanche Peak nuclear data-center PPA queue
        "NRG",   # NRG Energy — TX data-center load
        "D",     # Dominion — VA data-center alley
        "SO",    # Southern Co — GA Vogtle nuclear
        "AEP",   # American Electric Power — TX/OK
        "EXC",   # Exelon — IL/MD nuclear-adjacent
        "ETR",   # Entergy — LA hyperscaler load
    ],
    "power_infra_equipment": [
        "GEV",   # GE Vernova — grid + turbines
        "BE",    # Bloom Energy — distributed fuel cells (SA LP position)
        "PWR",   # Quanta Services — transmission build
    ],
    "miner_pivot": [
        "APLD",  # Applied Digital (SA LP position)
        "IREN",  # Iris Energy (SA LP position)
        "CORZ",  # Core Scientific (SA LP position)
        "CLSK",  # CleanSpark (SA LP position)
    ],
}


def _all_tickers_with_category() -> list[tuple[str, str]]:
    """Flatten POWER_TICKERS into ``[(ticker, category), ...]`` order."""
    return [
        (t, cat)
        for cat, tickers in POWER_TICKERS.items()
        for t in tickers
    ]


@dataclass
class TickerSnapshot:
    """Per-ticker fields the compiler writes. Mirrors the JSON output row."""

    ticker: str
    category: str
    price_usd: float | None
    market_cap_usd: float | None
    trailing_pe: float | None
    trailing_eps: float | None
    next_earnings_date: str | None
    fetch_status: str  # "ok" | "error"
    error_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ticker": self.ticker,
            "category": self.category,
            "price_usd": self.price_usd,
            "market_cap_usd": self.market_cap_usd,
            "trailing_pe": self.trailing_pe,
            "trailing_eps": self.trailing_eps,
            "next_earnings_date": self.next_earnings_date,
            "fetch_status": self.fetch_status,
        }
        if self.error_reason:
            d["error_reason"] = self.error_reason
        return d


# ---------------------------------------------------------------------------
# yfinance adapter (DI for tests)
# ---------------------------------------------------------------------------


def _default_yf_fetcher(ticker: str) -> dict[str, Any]:
    """Default yfinance adapter — fetches ``.info`` for one ticker.

    Lazy import keeps yfinance off the test path when callers inject a
    stub. yfinance occasionally raises on malformed tickers or rate
    limits; callers wrap.
    """
    import yfinance as yf  # noqa: PLC0415

    tkr = yf.Ticker(ticker)
    info = tkr.info or {}
    # next-earnings-date comes from .calendar (sometimes a DataFrame,
    # sometimes a dict, sometimes None). Normalize to ISO string or None.
    next_earnings = None
    try:
        cal = tkr.calendar
        if cal is not None:
            # Two shapes seen in the wild:
            #   - pandas DataFrame with "Earnings Date" column
            #   - dict with "Earnings Date" key
            if hasattr(cal, "to_dict"):
                cal = cal.to_dict()
            earnings_dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if earnings_dates:
                # Could be list[datetime] or single datetime
                first = earnings_dates[0] if isinstance(earnings_dates, list) else earnings_dates
                if hasattr(first, "isoformat"):
                    next_earnings = first.isoformat()[:10]
                else:
                    next_earnings = str(first)[:10]
    except (AttributeError, KeyError, IndexError, TypeError):
        next_earnings = None
    info["_next_earnings_date"] = next_earnings
    return info


def _snapshot_from_info(ticker: str, category: str, info: dict[str, Any]) -> TickerSnapshot:
    """Normalize a yfinance ``.info`` dict into a TickerSnapshot."""
    price = (
        info.get("regularMarketPrice")
        or info.get("currentPrice")
        or info.get("previousClose")
    )
    return TickerSnapshot(
        ticker=ticker,
        category=category,
        price_usd=float(price) if price is not None else None,
        market_cap_usd=(
            float(info["marketCap"]) if info.get("marketCap") is not None else None
        ),
        trailing_pe=(
            float(info["trailingPE"]) if info.get("trailingPE") is not None else None
        ),
        trailing_eps=(
            float(info["trailingEps"]) if info.get("trailingEps") is not None else None
        ),
        next_earnings_date=info.get("_next_earnings_date"),
        fetch_status="ok",
    )


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def compose(
    *,
    out_path: Path | None = None,
    yf_fetcher: Callable[[str], dict[str, Any]] = _default_yf_fetcher,
    now_iso_fn: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"),
    tickers: dict[str, list[str]] | None = None,
) -> TraceEntry:
    """Fetch the curated power-sector ticker snapshots and write the JSON.

    Args:
        out_path: when set, the JSON output is written to this path AFTER
            being composed. Parent dir is created if missing. Pass None
            to compose-without-writing (useful for tests + dry-runs).
        yf_fetcher: per-ticker info fetcher. DI for tests.
        now_iso_fn: clock injection for deterministic test output.
        tickers: override the curated POWER_TICKERS catalog (tests).

    Returns:
        TraceEntry whose ``output`` is the full JSON payload (also written
        to ``out_path`` when set).
    """
    catalog = tickers if tickers is not None else POWER_TICKERS
    flat = [(t, cat) for cat, ts in catalog.items() for t in ts]
    snapshots: list[TickerSnapshot] = []
    errors: list[dict[str, str]] = []

    for ticker, category in flat:
        try:
            info = yf_fetcher(ticker)
            snapshots.append(_snapshot_from_info(ticker, category, info))
        except Exception as e:  # noqa: BLE001 — best-effort per ticker
            err = f"{type(e).__name__}: {e}"
            errors.append({"ticker": ticker, "reason": err})
            snapshots.append(
                TickerSnapshot(
                    ticker=ticker,
                    category=category,
                    price_usd=None,
                    market_cap_usd=None,
                    trailing_pe=None,
                    trailing_eps=None,
                    next_earnings_date=None,
                    fetch_status="error",
                    error_reason=err,
                )
            )

    n_ok = sum(1 for s in snapshots if s.fetch_status == "ok")

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "fetched_at": now_iso_fn(),
        "categories": {cat: list(ts) for cat, ts in catalog.items()},
        "tickers": [s.to_dict() for s in snapshots],
        "n_tickers_attempted": len(flat),
        "n_tickers_ok": n_ok,
        "errors": errors,
    }

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return TraceEntry(
        tool=TOOL,
        inputs={
            "out_path": str(out_path) if out_path else None,
            "n_categories": len(catalog),
        },
        output=payload,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.tier3.power_sector",
        description=__doc__,
    )
    p.add_argument(
        "--out",
        default="ledgers/thematic/tier3/power_sector.json",
        help="Output JSON path. Default: ledgers/thematic/tier3/power_sector.json",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compose + emit the TraceEntry but skip the file write.",
    )
    return p


def main() -> None:
    args = _build_arg_parser().parse_args()
    out_path = None if args.dry_run else Path(args.out)
    trace = compose(out_path=out_path)
    emit(trace)


if __name__ == "__main__":
    main()
