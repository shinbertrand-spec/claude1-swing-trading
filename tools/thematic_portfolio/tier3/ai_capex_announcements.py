"""Hyperscaler AI-capex Tier 3 signal compiler.

V1 scope: pulls the most-recent 3 fiscal years of `PaymentsToAcquirePropertyPlantAndEquipment`
(the XBRL concept for capex / property-plant-equipment purchases) from each
of the 5 named hyperscalers' cash flow statements via ``edgartools``. The
trend line — accelerating / flat / decelerating — is the load-bearing
signal for SA LP's "demand for AI compute drives the binding-constraint
thesis on power" framing.

What this is NOT (v1):

* Not real-time. Pulls FY-annual data from latest available 10-K. Quarterly
  capex is noisier and requires ``Company.get_quarterly_financials()`` —
  deferred to v2.
* Not "capex guidance" in the forward-looking sense. Captures actuals
  reported in the cash flow statement; forward guidance lives in the
  MD&A narrative section which requires regex / LLM extraction — deferred
  to v2.
* Not text mining. Mention counts for "AI" / "data center" / "compute"
  inside the 10-K body are valuable color but require pulling the filing
  text — deferred to v2 if signal quality justifies the EDGAR HTTP cost.

CLI::

    uv run python -m tools.thematic_portfolio.tier3.ai_capex_announcements \\
        --out ledgers/thematic/tier3/ai_capex_announcements.json

Library::

    from tools.thematic_portfolio.tier3.ai_capex_announcements import compose
    trace = compose(out_path=Path("..."))
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ...cli import emit
from ...contract import TraceEntry

TOOL = "tools/thematic_portfolio/tier3/ai_capex_announcements.py"
SCHEMA_VERSION = "1.0"

# Five hyperscalers. These are the demand side of SA LP's power thesis —
# their capex growth IS the AI buildout's revealed preference, and the
# load they place on utilities + grid is what makes power the binding
# constraint. Discipline same as power_sector: fund-identity-by-role.
HYPERSCALERS: dict[str, str] = {
    "MSFT": "Microsoft Corporation",
    "META": "Meta Platforms Inc.",
    "GOOGL": "Alphabet Inc.",
    "AMZN": "Amazon.com Inc.",
    "ORCL": "Oracle Corporation",
}

# Primary XBRL concept for capex / PPE purchases. Used by MSFT / META /
# GOOGL / ORCL. The us-gaap tag is the most-reliable single line on the
# cash flow statement for most US issuers.
CAPEX_XBRL_CONCEPT = "PaymentsToAcquirePropertyPlantAndEquipment"

# Fallback concept chain. Some issuers switched away from the canonical
# tag at various points:
# * AMZN switched to PaymentsToAcquireProductiveAssets in 2018+ (the
#   canonical concept has facts only for 2009-2017 in AMZN's filings).
# * Future hyperscalers may use yet other variants — extend this list
#   when a real-world fetch fails for a known good ticker.
CAPEX_XBRL_FALLBACK_CONCEPTS: tuple[str, ...] = (
    "PaymentsToAcquireProductiveAssets",
    "PaymentsForCapitalImprovements",
    "PaymentsToAcquirePropertyPlantAndEquipmentNet",
)

# Fiscal-year column header pattern emitted by edgartools' MultiPeriodStatement.
# Matches "FY 2025", "FY 2024", etc.
_FY_COL_RE = re.compile(r"^FY\s+(\d{4})$")


@dataclass
class CapexSnapshot:
    """Per-hyperscaler annual capex trend. Mirrors the JSON output row."""

    ticker: str
    company_name: str
    capex_by_fiscal_year_usd: dict[str, float]      # {"FY 2025": ..., "FY 2024": ...}
    latest_fy: str | None
    latest_fy_capex_usd: float | None
    prior_fy: str | None
    prior_fy_capex_usd: float | None
    yoy_change_pct: float | None                     # (latest - prior) / prior * 100
    trend: str | None                                # "accelerating" | "flat" | "decelerating"
    fetch_status: str
    error_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ticker": self.ticker,
            "company_name": self.company_name,
            "capex_by_fiscal_year_usd": self.capex_by_fiscal_year_usd,
            "latest_fy": self.latest_fy,
            "latest_fy_capex_usd": self.latest_fy_capex_usd,
            "prior_fy": self.prior_fy,
            "prior_fy_capex_usd": self.prior_fy_capex_usd,
            "yoy_change_pct": self.yoy_change_pct,
            "trend": self.trend,
            "fetch_status": self.fetch_status,
        }
        if self.error_reason:
            d["error_reason"] = self.error_reason
        return d


# ---------------------------------------------------------------------------
# Trend classification
# ---------------------------------------------------------------------------


def _classify_trend(yoy_change_pct: float | None) -> str | None:
    """Three-bucket classification of YoY capex change.

    Thresholds calibrated to hyperscaler norms 2020-2025:
    * accelerating: >= +20% YoY (the AI-buildout regime; MSFT/META/GOOGL
      all posted +30-80% YoY in 2024-2025)
    * decelerating: <= -10% YoY (capex cuts; significant signal — happened
      only in 2022 for META during its efficiency campaign)
    * flat: anything in between (steady-state or modest growth)
    """
    if yoy_change_pct is None:
        return None
    if yoy_change_pct >= 20.0:
        return "accelerating"
    if yoy_change_pct <= -10.0:
        return "decelerating"
    return "flat"


# ---------------------------------------------------------------------------
# edgartools adapter (DI for tests)
# ---------------------------------------------------------------------------


def _fy_capex_from_cash_flow_statement(
    ticker: str, edgar_module
) -> dict[str, float] | None:
    """Primary path — pull capex via the curated ``cash_flow_statement()``.

    Returns ``None`` when the primary XBRL concept is missing from the
    issuer's curated cash flow statement (caller falls back to the raw
    facts API).
    """
    df = edgar_module.Company(ticker).cash_flow_statement().to_dataframe()
    if CAPEX_XBRL_CONCEPT not in df.index:
        return None
    capex_row = df.loc[CAPEX_XBRL_CONCEPT]
    out: dict[str, float] = {}
    for col in df.columns:
        if not _FY_COL_RE.match(col):
            continue
        val = capex_row.get(col)
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if f != f:  # NaN
            continue
        out[col] = f
    return out


def _fy_capex_from_raw_facts(
    ticker: str, edgar_module
) -> dict[str, float] | None:
    """Fallback path — scan raw XBRL facts for the primary + fallback
    concepts, take the max value per fiscal year.

    XBRL facts can contain multiple values per fiscal year because of
    restatements, sub-segment breakouts, and cumulative interim values
    (Q1 / Q1+Q2 / Q1+Q2+Q3 may also appear with the same fiscal_year).
    Taking the max captures the full-year cumulative value because
    full-year capex is always >= any sub-period value.

    Returns ``None`` when none of the candidate concepts have any annual
    facts (caller surfaces this as an error).
    """
    facts = edgar_module.Company(ticker).get_facts()
    all_facts = facts.get_all_facts()

    candidate_concepts = {
        f"us-gaap:{CAPEX_XBRL_CONCEPT}",
        *[f"us-gaap:{c}" for c in CAPEX_XBRL_FALLBACK_CONCEPTS],
    }

    matched = [
        f for f in all_facts
        if f.concept in candidate_concepts
        and f.fiscal_period == "FY"
        and f.fiscal_year is not None
        and f.numeric_value is not None
        and not f.is_dimensioned  # exclude segment / dimensional breakouts
    ]
    if not matched:
        return None

    by_year: dict[int, float] = {}
    for f in matched:
        prev = by_year.get(f.fiscal_year)
        if prev is None or f.numeric_value > prev:
            by_year[f.fiscal_year] = float(f.numeric_value)

    return {f"FY {y}": v for y, v in sorted(by_year.items())}


def _default_capex_fetcher(ticker: str) -> dict[str, float]:
    """Default edgartools adapter — returns ``{fiscal_year_label: capex_usd}``.

    Two-path strategy:
    1. Primary: parse the curated ``cash_flow_statement()`` for the
       canonical XBRL concept (works for MSFT/META/GOOGL/ORCL).
    2. Fallback: scan raw XBRL facts for the canonical + variant concepts
       (works for AMZN, which uses PaymentsToAcquireProductiveAssets
       post-2017).

    Lazy-imports edgartools so the rest of this module can be imported
    without the dep installed (mirrors the thirteen_f / edgar_eps pattern).

    Raises an exception only when BOTH paths return no data — per-ticker
    errors are caught by the caller and surfaced in the snapshot's
    ``error_reason`` field; they never abort the run.
    """
    import edgar  # noqa: PLC0415

    # Identity is set once at the EDGAR client level.
    identity = (
        __import__("os").environ.get("EDGAR_IDENTITY")
        or "Bertrand Shin shinbertrand@gmail.com"
    )
    edgar.set_identity(identity)

    primary = _fy_capex_from_cash_flow_statement(ticker, edgar)
    if primary:
        return primary

    fallback = _fy_capex_from_raw_facts(ticker, edgar)
    if fallback:
        return fallback

    raise KeyError(
        f"XBRL capex concept missing for {ticker}. Tried primary "
        f"{CAPEX_XBRL_CONCEPT} on cash flow statement + fallbacks "
        f"{CAPEX_XBRL_FALLBACK_CONCEPTS} on raw facts."
    )


def _snapshot_from_capex_dict(
    ticker: str, company_name: str, capex_by_fy: dict[str, float]
) -> CapexSnapshot:
    """Normalize a ``{fy_label: capex_usd}`` dict into a CapexSnapshot."""
    if not capex_by_fy:
        return CapexSnapshot(
            ticker=ticker,
            company_name=company_name,
            capex_by_fiscal_year_usd={},
            latest_fy=None,
            latest_fy_capex_usd=None,
            prior_fy=None,
            prior_fy_capex_usd=None,
            yoy_change_pct=None,
            trend=None,
            fetch_status="error",
            error_reason="no_fy_columns_returned",
        )

    # Sort by FY year ascending so [-1] is the most-recent.
    sorted_items = sorted(
        capex_by_fy.items(),
        key=lambda kv: int(_FY_COL_RE.match(kv[0]).group(1)),  # type: ignore[union-attr]
    )
    latest_fy, latest_val = sorted_items[-1]
    prior_fy, prior_val = (sorted_items[-2] if len(sorted_items) >= 2 else (None, None))

    yoy: float | None = None
    if prior_val is not None and prior_val != 0:
        yoy = (latest_val - prior_val) / prior_val * 100.0

    return CapexSnapshot(
        ticker=ticker,
        company_name=company_name,
        capex_by_fiscal_year_usd=dict(sorted_items),
        latest_fy=latest_fy,
        latest_fy_capex_usd=latest_val,
        prior_fy=prior_fy,
        prior_fy_capex_usd=prior_val,
        yoy_change_pct=yoy,
        trend=_classify_trend(yoy),
        fetch_status="ok",
    )


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def compose(
    *,
    out_path: Path | None = None,
    capex_fetcher: Callable[[str], dict[str, float]] = _default_capex_fetcher,
    now_iso_fn: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"),
    hyperscalers: dict[str, str] | None = None,
) -> TraceEntry:
    """Pull per-hyperscaler annual capex trend, write the JSON.

    Args:
        out_path: write target when set; pass None for compose-without-writing.
        capex_fetcher: per-ticker fetcher returning ``{fy_label: capex_usd}``.
            DI for tests.
        now_iso_fn: clock injection.
        hyperscalers: override the HYPERSCALERS catalog (tests).

    Returns:
        TraceEntry whose ``output`` is the full JSON payload.
    """
    catalog = hyperscalers if hyperscalers is not None else HYPERSCALERS

    snapshots: list[CapexSnapshot] = []
    errors: list[dict[str, str]] = []

    for ticker, name in catalog.items():
        try:
            capex_by_fy = capex_fetcher(ticker)
            snapshots.append(_snapshot_from_capex_dict(ticker, name, capex_by_fy))
        except Exception as e:  # noqa: BLE001 — best-effort per ticker
            err = f"{type(e).__name__}: {e}"
            errors.append({"ticker": ticker, "reason": err})
            snapshots.append(
                CapexSnapshot(
                    ticker=ticker,
                    company_name=name,
                    capex_by_fiscal_year_usd={},
                    latest_fy=None,
                    latest_fy_capex_usd=None,
                    prior_fy=None,
                    prior_fy_capex_usd=None,
                    yoy_change_pct=None,
                    trend=None,
                    fetch_status="error",
                    error_reason=err,
                )
            )

    n_ok = sum(1 for s in snapshots if s.fetch_status == "ok")

    # Aggregate: median YoY across the successful fetches; thesis signal
    # based on how many are "accelerating" vs "decelerating".
    ok_yoy = [s.yoy_change_pct for s in snapshots if s.yoy_change_pct is not None]
    median_yoy: float | None = None
    if ok_yoy:
        ok_yoy_sorted = sorted(ok_yoy)
        mid = len(ok_yoy_sorted) // 2
        median_yoy = (
            ok_yoy_sorted[mid]
            if len(ok_yoy_sorted) % 2 == 1
            else (ok_yoy_sorted[mid - 1] + ok_yoy_sorted[mid]) / 2.0
        )

    trends = [s.trend for s in snapshots if s.trend is not None]
    n_accel = sum(1 for t in trends if t == "accelerating")
    n_decel = sum(1 for t in trends if t == "decelerating")

    if not trends:
        thesis_signal = "no_data"
    elif n_accel >= 3:
        thesis_signal = "accelerating"
    elif n_decel >= 3:
        thesis_signal = "decelerating"
    else:
        thesis_signal = "mixed"

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "fetched_at": now_iso_fn(),
        "xbrl_concept": CAPEX_XBRL_CONCEPT,
        "hyperscalers": [s.to_dict() for s in snapshots],
        "aggregate": {
            "n_ok": n_ok,
            "n_attempted": len(catalog),
            "median_yoy_change_pct": median_yoy,
            "n_accelerating": n_accel,
            "n_decelerating": n_decel,
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
            "n_hyperscalers": len(catalog),
        },
        output=payload,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.tier3.ai_capex_announcements",
        description=__doc__,
    )
    p.add_argument(
        "--out",
        default="ledgers/thematic/tier3/ai_capex_announcements.json",
        help=(
            "Output JSON path. Default: "
            "ledgers/thematic/tier3/ai_capex_announcements.json"
        ),
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
