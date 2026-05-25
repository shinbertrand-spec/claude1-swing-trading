"""Energy-futures Tier 3 signal compiler.

V1 scope: pulls last close + 30d / 90d / YTD % change for a curated set of
energy-input symbols that bear on the AI-power-demand thesis. The trend
across these symbols is what's load-bearing — rising natgas + uranium +
utility tape over 90d is consistent with SA LP's "power is the binding
constraint" framing; falling spreads weaken it.

Data source: ``yfinance`` (no API key required). EIA's v2 API requires a
free key and lives behind a different module if precision matters; this
v1 trades that off for zero-config public-data access.

Symbol categories (encode INPUT-IDENTITY, not fund positions — same
discipline as power_sector / ai_capex):

* ``natgas`` — Henry Hub front-month + ETF proxies; primary US power-gen
  feedstock for incremental data-center load.
* ``uranium`` — uranium ETFs + producers; nuclear-restart thesis (CEG TMI,
  VST Comanche Peak, GOOGL Kairos SMR PPA).
* ``crude_oil`` — WTI front-month + sector ETF; broad energy reference
  for macro hedge interpretation.
* ``power_proxy`` — utility-sector ETFs; there is no retail-accessible
  PJM-W / ERCOT power futures price, so the utility-equity tape is the
  cleanest no-key proxy.

Output JSON shape (locked in tests; v2 may add fields, never remove)::

    {
      "schema_version": "1.0",
      "fetched_at": "<ISO-8601 UTC>",
      "categories": {
        "natgas": ["NG=F", "UNG", "UNL"],
        "uranium": ["URA", "URNM", "CCJ", "UEC"],
        "crude_oil": ["CL=F", "XLE"],
        "power_proxy": ["XLU", "RYU"]
      },
      "symbols": [
        {
          "symbol": "NG=F",
          "category": "natgas",
          "last_close_usd": 3.42,
          "change_30d_pct": 8.1,
          "change_90d_pct": 14.2,
          "change_ytd_pct": -2.3,
          "fetch_status": "ok"
        },
        ...
      ],
      "aggregate": {
        "n_ok": 10,
        "n_attempted": 11,
        "median_change_90d_pct_by_category": {"natgas": 12.0, ...},
        "thesis_signal": "supportive" | "mixed" | "weakening" | "no_data"
      },
      "errors": [{"symbol": "X", "reason": "..."}]
    }

Per-symbol failures NEVER crash the compiler — they land in ``errors``
with ``fetch_status: "error"`` and the symbol still appears in
``symbols[]`` so the LLM sees the gap.

CLI::

    uv run python -m tools.thematic_portfolio.tier3.energy_futures \\
        --out ledgers/thematic/tier3/energy_futures.json

Library::

    from tools.thematic_portfolio.tier3.energy_futures import compose
    trace = compose(out_path=Path("..."))
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

TOOL = "tools/thematic_portfolio/tier3/energy_futures.py"
SCHEMA_VERSION = "1.0"

# Curated symbols. Categories encode INPUT-IDENTITY (natgas / uranium /
# crude / power-proxy) and are stable across cycles — fund-position
# overlap with these is illustrative, not load-bearing.
ENERGY_SYMBOLS: dict[str, list[str]] = {
    "natgas": [
        "NG=F",  # Henry Hub continuous front-month ($/MMBtu)
        "UNG",   # US Natural Gas Fund (spot proxy)
        "UNL",   # US 12-Month Natural Gas Fund (curve proxy)
    ],
    "uranium": [
        "URA",   # Global X Uranium ETF
        "URNM",  # Sprott Uranium Miners ETF
        "CCJ",   # Cameco — major uranium producer
        "UEC",   # Uranium Energy Corp
    ],
    "crude_oil": [
        "CL=F",  # WTI continuous front-month ($/bbl)
        "XLE",   # Energy Select Sector SPDR
    ],
    "power_proxy": [
        "XLU",   # Utilities Select Sector SPDR (broad utility tape)
        "RYU",   # Invesco S&P 500 Equal Weight Utilities
    ],
}


@dataclass
class SymbolSnapshot:
    """Per-symbol fields the compiler writes. Mirrors the JSON output row."""

    symbol: str
    category: str
    last_close_usd: float | None
    change_30d_pct: float | None
    change_90d_pct: float | None
    change_ytd_pct: float | None
    fetch_status: str  # "ok" | "error"
    error_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "symbol": self.symbol,
            "category": self.category,
            "last_close_usd": self.last_close_usd,
            "change_30d_pct": self.change_30d_pct,
            "change_90d_pct": self.change_90d_pct,
            "change_ytd_pct": self.change_ytd_pct,
            "fetch_status": self.fetch_status,
        }
        if self.error_reason:
            d["error_reason"] = self.error_reason
        return d


# ---------------------------------------------------------------------------
# yfinance adapter (DI for tests)
# ---------------------------------------------------------------------------


def _default_yf_fetcher(symbol: str) -> dict[str, Any]:
    """Default yfinance adapter — returns history + last-close for one symbol.

    Returns a dict with shape::

        {
          "last_close": float,
          "change_30d_pct": float | None,
          "change_90d_pct": float | None,
          "change_ytd_pct": float | None,
        }

    The fetcher pulls ~1y of daily history (enough to span YTD on any
    trading day) and computes the three lookback returns deterministically.
    Lazy import keeps yfinance off the test path.
    """
    import yfinance as yf  # noqa: PLC0415

    tkr = yf.Ticker(symbol)
    # ~1y is enough to cover YTD on any trading day; "period" doesn't have
    # an exact YTD option in all yfinance versions, so we slice manually.
    hist = tkr.history(period="1y", auto_adjust=False)
    if hist is None or hist.empty:
        raise ValueError(f"no_history_returned for {symbol}")

    closes = hist["Close"]
    last_close = float(closes.iloc[-1])

    def _lookback_pct(n_trading_days: int) -> float | None:
        if len(closes) <= n_trading_days:
            return None
        prior = float(closes.iloc[-1 - n_trading_days])
        if prior == 0:
            return None
        return (last_close - prior) / prior * 100.0

    # ~21 trading days / month, ~63 / 3 months.
    change_30d = _lookback_pct(21)
    change_90d = _lookback_pct(63)

    # YTD: find the first close in the current calendar year.
    last_ts = hist.index[-1]
    year_start = hist.index[hist.index.year == last_ts.year]
    change_ytd: float | None = None
    if len(year_start) > 0:
        ytd_open = float(closes.loc[year_start[0]])
        if ytd_open != 0:
            change_ytd = (last_close - ytd_open) / ytd_open * 100.0

    return {
        "last_close": last_close,
        "change_30d_pct": change_30d,
        "change_90d_pct": change_90d,
        "change_ytd_pct": change_ytd,
    }


def _snapshot_from_history(
    symbol: str, category: str, hist: dict[str, Any]
) -> SymbolSnapshot:
    """Normalize a fetcher output dict into a SymbolSnapshot."""
    return SymbolSnapshot(
        symbol=symbol,
        category=category,
        last_close_usd=(
            float(hist["last_close"]) if hist.get("last_close") is not None else None
        ),
        change_30d_pct=(
            float(hist["change_30d_pct"])
            if hist.get("change_30d_pct") is not None
            else None
        ),
        change_90d_pct=(
            float(hist["change_90d_pct"])
            if hist.get("change_90d_pct") is not None
            else None
        ),
        change_ytd_pct=(
            float(hist["change_ytd_pct"])
            if hist.get("change_ytd_pct") is not None
            else None
        ),
        fetch_status="ok",
    )


# ---------------------------------------------------------------------------
# Aggregate-signal classification
# ---------------------------------------------------------------------------


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _classify_thesis_signal(
    median_by_category: dict[str, float | None],
) -> str:
    """Three-bucket aggregate classification across categories.

    The AI-power-demand thesis is supportive when natgas + uranium + power
    proxies are all rising over 90d (the demand pull is showing in input
    costs + utility tape). It's weakening when 2+ categories are negative.

    Buckets:
    * supportive: at least 2 of {natgas, uranium, power_proxy} >= +5% 90d
    * weakening: at least 2 of {natgas, uranium, power_proxy} <= -5% 90d
    * mixed: anything else with data
    * no_data: when zero categories have data
    """
    core_keys = ("natgas", "uranium", "power_proxy")
    core_values = [
        median_by_category.get(k)
        for k in core_keys
        if median_by_category.get(k) is not None
    ]
    if not core_values:
        return "no_data"

    n_supportive = sum(1 for v in core_values if v is not None and v >= 5.0)
    n_weak = sum(1 for v in core_values if v is not None and v <= -5.0)

    if n_supportive >= 2:
        return "supportive"
    if n_weak >= 2:
        return "weakening"
    return "mixed"


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def compose(
    *,
    out_path: Path | None = None,
    yf_fetcher: Callable[[str], dict[str, Any]] = _default_yf_fetcher,
    now_iso_fn: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"),
    symbols: dict[str, list[str]] | None = None,
) -> TraceEntry:
    """Fetch the curated energy-symbol snapshots and write the JSON.

    Args:
        out_path: write target when set; pass None to compose-without-writing.
        yf_fetcher: per-symbol history fetcher (DI for tests).
        now_iso_fn: clock injection.
        symbols: override the ENERGY_SYMBOLS catalog (tests).

    Returns:
        TraceEntry whose ``output`` is the full JSON payload.
    """
    catalog = symbols if symbols is not None else ENERGY_SYMBOLS
    flat = [(s, cat) for cat, syms in catalog.items() for s in syms]
    snapshots: list[SymbolSnapshot] = []
    errors: list[dict[str, str]] = []

    for symbol, category in flat:
        try:
            hist = yf_fetcher(symbol)
            snapshots.append(_snapshot_from_history(symbol, category, hist))
        except Exception as e:  # noqa: BLE001 — best-effort per symbol
            err = f"{type(e).__name__}: {e}"
            errors.append({"symbol": symbol, "reason": err})
            snapshots.append(
                SymbolSnapshot(
                    symbol=symbol,
                    category=category,
                    last_close_usd=None,
                    change_30d_pct=None,
                    change_90d_pct=None,
                    change_ytd_pct=None,
                    fetch_status="error",
                    error_reason=err,
                )
            )

    n_ok = sum(1 for s in snapshots if s.fetch_status == "ok")

    # Median 90d change per category — the load-bearing signal for the
    # thesis classifier.
    median_by_category: dict[str, float | None] = {}
    for cat in catalog:
        cat_vals = [
            s.change_90d_pct
            for s in snapshots
            if s.category == cat and s.change_90d_pct is not None
        ]
        median_by_category[cat] = _median(cat_vals)

    thesis_signal = _classify_thesis_signal(median_by_category)

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "fetched_at": now_iso_fn(),
        "categories": {cat: list(syms) for cat, syms in catalog.items()},
        "symbols": [s.to_dict() for s in snapshots],
        "aggregate": {
            "n_ok": n_ok,
            "n_attempted": len(flat),
            "median_change_90d_pct_by_category": median_by_category,
            "thesis_signal": thesis_signal,
        },
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
        prog="tools.thematic_portfolio.tier3.energy_futures",
        description=__doc__,
    )
    p.add_argument(
        "--out",
        default="ledgers/thematic/tier3/energy_futures.json",
        help="Output JSON path. Default: ledgers/thematic/tier3/energy_futures.json",
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
