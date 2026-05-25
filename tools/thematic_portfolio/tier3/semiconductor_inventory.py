"""Semiconductor-inventory Tier 3 signal compiler.

V2 scope (v1 was rejected — TrendForce / DRAMeXchange paywalled). Combines
two free data sources to surface the chip-supply tightness signal that
bears on the SA LP put-complex thesis:

* **Momentum (yfinance, all tickers)** — last close + 30d / 90d / YTD %
  change for the semis ETFs + foundry / GPU / memory names. Captures the
  broad-tape view; a strong SOXX / SMH tape implies semis are still being
  bid up.

* **Inventory metrics (edgartools, US 10-Q filers only)** — most-recent
  10-Q's InventoryNet + CostOfRevenue (or equivalent). Pulls both the
  latest quarter end AND the prior quarter end from the SAME filing
  (balance sheets always show the comparable period), so we get a clean
  sequential QoQ inventory change without a second EDGAR fetch.

  US 10-Q filers covered: NVDA, AMD, MU. TSM (foreign issuer, files 6-K)
  + ETFs do not get the inventory lookup; their rows still appear in the
  output with ``inventory_qoq_change_pct: null``.

Why this matters for the SA LP put-complex thesis:

* Rising inventory days + softening semis tape = looser supply / demand
  weakening = supportive of the put-complex thesis (the SHORT side of
  SA LP's barbell expects a chip-cycle correction).
* Flat-or-falling inventory days + firming semis tape = tight supply /
  demand strong = the put-complex looks ill-timed.

Output JSON shape (locked in tests; v2 may add fields, never remove)::

    {
      "schema_version": "1.0",
      "fetched_at": "...",
      "categories": {
        "semis_index": ["SOXX", "SMH"],
        "foundry": ["TSM"],
        "gpu": ["NVDA", "AMD"],
        "memory_hbm": ["MU"]
      },
      "inventory_fetch_tickers": ["NVDA", "AMD", "MU"],
      "symbols": [
        {
          "symbol": "NVDA",
          "category": "gpu",
          "last_close_usd": 1234.5,
          "change_30d_pct": 8.1,
          "change_90d_pct": 25.0,
          "change_ytd_pct": 40.2,
          "inventory_latest_usd": 25_797_000_000,
          "inventory_latest_period": "2026-04-26",
          "inventory_prior_usd": 21_403_000_000,
          "inventory_prior_period": "2026-01-25",
          "inventory_qoq_change_pct": 20.53,
          "cogs_latest_usd": 20_458_000_000,
          "inventory_days_latest": 114.7,
          "fetch_status": "ok"
        },
        ...
      ],
      "aggregate": {
        "n_ok": 6,
        "n_attempted": 6,
        "median_change_90d_pct_by_category": {...},
        "median_inventory_qoq_change_pct": 6.7,
        "thesis_signal": "chip_supply_tight" | "chip_supply_loose" | "mixed" | "no_data"
      },
      "errors": []
    }

Per-symbol failures NEVER crash the compiler — they land in ``errors``
with ``fetch_status: "error"`` and the symbol still appears in
``symbols[]`` so the LLM sees the gap explicitly.

CLI::

    uv run python -m tools.thematic_portfolio.tier3.semiconductor_inventory \\
        --out ledgers/thematic/tier3/semiconductor_inventory.json
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

TOOL = "tools/thematic_portfolio/tier3/semiconductor_inventory.py"
SCHEMA_VERSION = "1.0"

# Curated tickers. Categories encode SUPPLY-CHAIN role; same fund-identity
# discipline as power_sector / ai_capex / energy_futures.
SEMI_TICKERS: dict[str, list[str]] = {
    "semis_index": ["SOXX", "SMH"],         # broad-tape momentum proxies
    "foundry": ["TSM"],                      # AI-buildout substrate (TSMC ADR)
    "gpu": ["NVDA", "AMD"],                  # primary AI demand pullers
    "memory_hbm": ["MU"],                    # HBM-exposed US-listed name
}

# Subset of SEMI_TICKERS that file US 10-Q with the standard
# InventoryNet / CostOfRevenue concept stack. Other tickers (ETFs, TSM
# foreign issuer) skip the edgartools call but still appear in output.
INVENTORY_FETCH_TICKERS: frozenset[str] = frozenset({"NVDA", "AMD", "MU"})

INVENTORY_XBRL_CONCEPT = "us-gaap_InventoryNet"

# COGS concept priority order. Different issuers use different variants;
# we take the first match per ticker.
COGS_XBRL_CONCEPTS: tuple[str, ...] = (
    "us-gaap_CostOfRevenue",                         # NVDA
    "us-gaap_CostOfGoodsAndServicesSold",            # AMD, MU
    "us-gaap_CostOfGoodsSold",                       # older filings
)

# Standard quarter length used for inventory-days normalization. The exact
# fiscal-quarter day count varies (13 weeks = 91 days for most US issuers;
# 91 or 92 calendar days). 91 is the conventional approximation.
DAYS_IN_QUARTER = 91


@dataclass
class SymbolSnapshot:
    """Per-symbol snapshot. Combines momentum + (optional) inventory."""

    symbol: str
    category: str
    # Momentum block (yfinance)
    last_close_usd: float | None
    change_30d_pct: float | None
    change_90d_pct: float | None
    change_ytd_pct: float | None
    # Inventory block (edgartools, US 10-Q filers only — else None)
    inventory_latest_usd: float | None
    inventory_latest_period: str | None
    inventory_prior_usd: float | None
    inventory_prior_period: str | None
    inventory_qoq_change_pct: float | None
    cogs_latest_usd: float | None
    inventory_days_latest: float | None
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
            "inventory_latest_usd": self.inventory_latest_usd,
            "inventory_latest_period": self.inventory_latest_period,
            "inventory_prior_usd": self.inventory_prior_usd,
            "inventory_prior_period": self.inventory_prior_period,
            "inventory_qoq_change_pct": self.inventory_qoq_change_pct,
            "cogs_latest_usd": self.cogs_latest_usd,
            "inventory_days_latest": self.inventory_days_latest,
            "fetch_status": self.fetch_status,
        }
        if self.error_reason:
            d["error_reason"] = self.error_reason
        return d


# ---------------------------------------------------------------------------
# yfinance momentum adapter (DI for tests)
# ---------------------------------------------------------------------------


def _default_momentum_fetcher(symbol: str) -> dict[str, Any]:
    """Default yfinance adapter — returns last_close + lookback returns.

    Same shape as ``tools.thematic_portfolio.tier3.energy_futures._default_yf_fetcher``.
    Reused-style logic; not a literal re-export because the symbol set is
    different and the test stubs need to inject distinctly per module.
    """
    import yfinance as yf  # noqa: PLC0415

    tkr = yf.Ticker(symbol)
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

    change_30d = _lookback_pct(21)
    change_90d = _lookback_pct(63)

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


# ---------------------------------------------------------------------------
# edgartools inventory adapter (DI for tests)
# ---------------------------------------------------------------------------


def _top_level_row_value(df, concept: str, period_col: str) -> float | None:
    """Find the top-level (non-segmented) row for ``concept`` and return
    its value at ``period_col``, or None when absent.

    edgartools' quarterly_financials dataframes can include segmented
    breakouts under the same concept (e.g. MU's multi-product COGS
    breakouts). We want only the top-level total.

    The ``dimension`` column in edgartools dataframes is normally a
    boolean (False = top-level, True = segmented). Some versions /
    serializations also surface NaN / empty-string / em-dash for the
    top-level — we accept all of those representations.
    """
    if "concept" not in df.columns or period_col not in df.columns:
        return None
    rows = df[df["concept"] == concept]
    if "dimension" in rows.columns:
        mask = (
            rows["dimension"].isna()
            | (rows["dimension"] == "")
            | (rows["dimension"] == "—")
            | (rows["dimension"] == False)  # noqa: E712 — boolean equality is the API
        )
        rows = rows[mask]
    if rows.empty:
        return None
    val = rows.iloc[0].get(period_col)
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _default_inventory_fetcher(ticker: str) -> dict[str, Any]:
    """Pull inventory + COGS for the latest 10-Q via edgartools.

    Returns a dict with shape::

        {
          "inventory_latest_usd": float,
          "inventory_latest_period": "YYYY-MM-DD",
          "inventory_prior_usd": float | None,
          "inventory_prior_period": "YYYY-MM-DD" | None,
          "cogs_latest_usd": float | None,
        }

    Raises a meaningful error when InventoryNet is missing entirely
    (per-symbol error catch in compose() converts that to fetch_status=error).
    """
    import edgar  # noqa: PLC0415

    identity = (
        __import__("os").environ.get("EDGAR_IDENTITY")
        or "Bertrand Shin shinbertrand@gmail.com"
    )
    edgar.set_identity(identity)

    qf = edgar.Company(ticker).get_quarterly_financials()
    bs_df = qf.balance_sheet().to_dataframe()
    is_df = qf.income_statement().to_dataframe()

    # Balance sheet period columns are bare ISO dates ("2026-04-26").
    bs_periods = [
        c for c in bs_df.columns
        if isinstance(c, str) and len(c) == 10 and c[4] == "-" and c[7] == "-"
    ]
    if not bs_periods:
        raise ValueError(f"no_period_columns_on_balance_sheet for {ticker}")

    latest_period = bs_periods[0]
    prior_period = bs_periods[1] if len(bs_periods) >= 2 else None

    inv_latest = _top_level_row_value(bs_df, INVENTORY_XBRL_CONCEPT, latest_period)
    if inv_latest is None:
        raise KeyError(
            f"{INVENTORY_XBRL_CONCEPT} missing from {ticker} balance sheet for {latest_period}"
        )
    inv_prior = (
        _top_level_row_value(bs_df, INVENTORY_XBRL_CONCEPT, prior_period)
        if prior_period
        else None
    )

    # Income statement period columns carry the "(Qx)" suffix.
    is_periods = [
        c for c in is_df.columns
        if isinstance(c, str) and len(c) >= 10 and "(Q" in c
    ]
    # Match the income-statement period whose date prefix == the latest
    # balance-sheet period.
    is_latest_col: str | None = None
    for c in is_periods:
        if c.startswith(latest_period):
            is_latest_col = c
            break
    cogs_latest: float | None = None
    if is_latest_col:
        for concept in COGS_XBRL_CONCEPTS:
            v = _top_level_row_value(is_df, concept, is_latest_col)
            if v is not None:
                cogs_latest = v
                break

    return {
        "inventory_latest_usd": inv_latest,
        "inventory_latest_period": latest_period,
        "inventory_prior_usd": inv_prior,
        "inventory_prior_period": prior_period,
        "cogs_latest_usd": cogs_latest,
    }


# ---------------------------------------------------------------------------
# Composer helpers
# ---------------------------------------------------------------------------


def _qoq_pct(latest: float | None, prior: float | None) -> float | None:
    if latest is None or prior is None or prior == 0:
        return None
    return (latest - prior) / prior * 100.0


def _inventory_days(inventory: float | None, cogs: float | None) -> float | None:
    if inventory is None or cogs is None or cogs == 0:
        return None
    return inventory / cogs * DAYS_IN_QUARTER


def _snapshot_from_data(
    *,
    symbol: str,
    category: str,
    momentum: dict[str, Any],
    inventory: dict[str, Any] | None,
) -> SymbolSnapshot:
    """Normalize fetcher outputs into a SymbolSnapshot."""
    inv_latest = inventory.get("inventory_latest_usd") if inventory else None
    inv_prior = inventory.get("inventory_prior_usd") if inventory else None
    cogs_latest = inventory.get("cogs_latest_usd") if inventory else None
    return SymbolSnapshot(
        symbol=symbol,
        category=category,
        last_close_usd=(
            float(momentum["last_close"]) if momentum.get("last_close") is not None else None
        ),
        change_30d_pct=(
            float(momentum["change_30d_pct"])
            if momentum.get("change_30d_pct") is not None
            else None
        ),
        change_90d_pct=(
            float(momentum["change_90d_pct"])
            if momentum.get("change_90d_pct") is not None
            else None
        ),
        change_ytd_pct=(
            float(momentum["change_ytd_pct"])
            if momentum.get("change_ytd_pct") is not None
            else None
        ),
        inventory_latest_usd=inv_latest,
        inventory_latest_period=(
            inventory.get("inventory_latest_period") if inventory else None
        ),
        inventory_prior_usd=inv_prior,
        inventory_prior_period=(
            inventory.get("inventory_prior_period") if inventory else None
        ),
        inventory_qoq_change_pct=_qoq_pct(inv_latest, inv_prior),
        cogs_latest_usd=cogs_latest,
        inventory_days_latest=_inventory_days(inv_latest, cogs_latest),
        fetch_status="ok",
    )


# ---------------------------------------------------------------------------
# Median helper + thesis-signal classifier
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
    semis_index_median_90d: float | None,
    median_inventory_qoq_change_pct: float | None,
) -> str:
    """Four-bucket aggregate classification.

    chip_supply_tight:
        semis_index momentum positive (>= 0% over 90d) AND
        median inventory QoQ change modest (<= +5%).
        Both signals consistent with strong demand absorbing supply.
    chip_supply_loose:
        semis_index momentum weak (<= -5% over 90d) OR
        median inventory QoQ change material (>= +15%).
        Either signal alone is enough to flip the bucket — inventory
        builds are leading indicators of demand softening.
    mixed:
        anything else where at least one signal exists.
    no_data:
        both signals are None.
    """
    if semis_index_median_90d is None and median_inventory_qoq_change_pct is None:
        return "no_data"

    # Loose-conditions check first (either trigger flips it)
    loose_semis = semis_index_median_90d is not None and semis_index_median_90d <= -5.0
    loose_inv = (
        median_inventory_qoq_change_pct is not None
        and median_inventory_qoq_change_pct >= 15.0
    )
    if loose_semis or loose_inv:
        return "chip_supply_loose"

    # Tight needs BOTH signals consistent (or one consistent + other None)
    tight_semis = semis_index_median_90d is None or semis_index_median_90d >= 0.0
    tight_inv = (
        median_inventory_qoq_change_pct is None
        or median_inventory_qoq_change_pct <= 5.0
    )
    if tight_semis and tight_inv:
        return "chip_supply_tight"

    return "mixed"


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def compose(
    *,
    out_path: Path | None = None,
    momentum_fetcher: Callable[[str], dict[str, Any]] = _default_momentum_fetcher,
    inventory_fetcher: Callable[[str], dict[str, Any]] = _default_inventory_fetcher,
    now_iso_fn: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"),
    tickers: dict[str, list[str]] | None = None,
    inventory_fetch_tickers: frozenset[str] | None = None,
) -> TraceEntry:
    """Compose the semis-inventory snapshot.

    Args:
        out_path: write target when set; pass None to compose-without-writing.
        momentum_fetcher: per-symbol yfinance adapter (DI).
        inventory_fetcher: per-ticker edgartools adapter (DI). Called only
            for tickers in ``inventory_fetch_tickers``.
        now_iso_fn: clock injection.
        tickers: override SEMI_TICKERS catalog (tests).
        inventory_fetch_tickers: override INVENTORY_FETCH_TICKERS (tests).

    Returns:
        TraceEntry whose ``output`` is the full JSON payload.
    """
    catalog = tickers if tickers is not None else SEMI_TICKERS
    inv_set = (
        inventory_fetch_tickers if inventory_fetch_tickers is not None
        else INVENTORY_FETCH_TICKERS
    )
    flat = [(s, cat) for cat, syms in catalog.items() for s in syms]
    snapshots: list[SymbolSnapshot] = []
    errors: list[dict[str, str]] = []

    for symbol, category in flat:
        # Momentum (all symbols)
        try:
            momentum = momentum_fetcher(symbol)
        except Exception as e:  # noqa: BLE001 — best-effort per symbol
            err = f"momentum: {type(e).__name__}: {e}"
            errors.append({"symbol": symbol, "reason": err})
            snapshots.append(
                SymbolSnapshot(
                    symbol=symbol,
                    category=category,
                    last_close_usd=None,
                    change_30d_pct=None,
                    change_90d_pct=None,
                    change_ytd_pct=None,
                    inventory_latest_usd=None,
                    inventory_latest_period=None,
                    inventory_prior_usd=None,
                    inventory_prior_period=None,
                    inventory_qoq_change_pct=None,
                    cogs_latest_usd=None,
                    inventory_days_latest=None,
                    fetch_status="error",
                    error_reason=err,
                )
            )
            continue

        # Inventory (only for the US 10-Q filer subset). Inventory failures
        # don't poison the row — momentum still lands ok; inventory fields
        # stay None and an error gets appended.
        inventory: dict[str, Any] | None = None
        if symbol in inv_set:
            try:
                inventory = inventory_fetcher(symbol)
            except Exception as e:  # noqa: BLE001 — per-symbol best-effort
                errors.append(
                    {"symbol": symbol, "reason": f"inventory: {type(e).__name__}: {e}"}
                )

        snapshots.append(
            _snapshot_from_data(
                symbol=symbol,
                category=category,
                momentum=momentum,
                inventory=inventory,
            )
        )

    n_ok = sum(1 for s in snapshots if s.fetch_status == "ok")

    # Median 90d change per category (load-bearing for the classifier)
    median_by_category: dict[str, float | None] = {}
    for cat in catalog:
        cat_vals = [
            s.change_90d_pct
            for s in snapshots
            if s.category == cat and s.change_90d_pct is not None
        ]
        median_by_category[cat] = _median(cat_vals)

    # Median inventory QoQ across the inventory-fetch tickers
    inv_qoq_vals = [
        s.inventory_qoq_change_pct
        for s in snapshots
        if s.inventory_qoq_change_pct is not None
    ]
    median_inv_qoq = _median(inv_qoq_vals)

    thesis_signal = _classify_thesis_signal(
        semis_index_median_90d=median_by_category.get("semis_index"),
        median_inventory_qoq_change_pct=median_inv_qoq,
    )

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "fetched_at": now_iso_fn(),
        "categories": {cat: list(syms) for cat, syms in catalog.items()},
        "inventory_fetch_tickers": sorted(inv_set),
        "symbols": [s.to_dict() for s in snapshots],
        "aggregate": {
            "n_ok": n_ok,
            "n_attempted": len(flat),
            "median_change_90d_pct_by_category": median_by_category,
            "median_inventory_qoq_change_pct": median_inv_qoq,
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
            "n_inventory_fetch_tickers": len(inv_set),
        },
        output=payload,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.tier3.semiconductor_inventory",
        description=__doc__,
    )
    p.add_argument(
        "--out",
        default="ledgers/thematic/tier3/semiconductor_inventory.json",
        help="Output JSON path. Default: ledgers/thematic/tier3/semiconductor_inventory.json",
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
