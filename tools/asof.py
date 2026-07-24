"""Shared as-of-date helpers — look-ahead exclusion as a data-layer contract (A3).

Per CLAUDE.md § Evaluation-honesty policies: look-ahead exclusion is enforced
IN CODE at the adapter layer, never in agent prompts. Backtest and eval paths
pass the simulation date (``as_of``), not "now". This module is the single
shared helper — adapters import from here instead of growing per-tool copies.

Two adapter classes exist (audited 2026-07-24, table in
``ledgers/improvements/2026-07-24-evaluation-honesty-batch-a/``):

* **PIT-capable** — can answer "what was knowable at date D": OHLCV via
  ``tools.backtest.data_cache`` (date-bounded), ``tools.fundamentals.
  pit_fundamentals`` (filed<=asof), ``insider_track_record`` / ``insider_
  events`` / ``form345_bulk`` (FILING_DATE), ``tools.backtest.security_master``
  (idx<=asof), ``tools.earnings_calendar`` (as_of anchor).
* **LIVE-ONLY** — structurally cannot replay the past (listed in
  :data:`LIVE_ONLY_ADAPTERS`). These MUST NOT feed a backtest or dated eval;
  a deterministic gate (:func:`assert_backtest_safe`) exists so pipelines can
  enforce that instead of relying on prompt discipline.
"""
from __future__ import annotations

import datetime as _dt
from typing import Callable, Iterable, Optional, TypeVar

TOOL = "tools/asof.py"

T = TypeVar("T")

# Adapters that can only observe "now". Key = module path; value = why +
# the PIT alternative a backtest/eval path must use instead.
LIVE_ONLY_ADAPTERS: dict[str, str] = {
    "tools.fundamentals.edgar_eps": (
        "always fetches latest TTM EPS; PIT alternative: "
        "tools.fundamentals.pit_fundamentals (filed<=asof)"
    ),
    "tools.news_research.x_scanner": (
        "live X search with a 60-min recency floor; no historical replay. "
        "Dated evals must replay from the stored corpus/news snapshots."
    ),
    "tools.x_common.twitterapi_client": (
        "advanced_search has no as-of bounds; live-only."
    ),
    "tools.auto_paper.screener": (
        "scrapes live finviz news/quote panels; no archive access."
    ),
    "tools.news_research.market_temperature": (
        "live CBOE/CNN/AAII/VIX gauges; as_of is an output timestamp, "
        "not an input filter."
    ),
}


def parse_as_of(value: "str | _dt.date | _dt.datetime") -> _dt.date:
    """Coerce an as-of value (ISO string / date / datetime) to a date."""
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    return _dt.date.fromisoformat(str(value)[:10])


def filter_as_of(
    items: Iterable[T],
    get_date: Callable[[T], "str | _dt.date | _dt.datetime | None"],
    as_of: "str | _dt.date | _dt.datetime",
) -> list[T]:
    """Return items whose date is known at ``as_of`` (date <= as_of).

    Items with a missing/unparseable date are EXCLUDED — an undated item
    cannot be proven knowable at ``as_of``, and silently including it is the
    exact look-ahead leak this contract exists to prevent.
    """
    cutoff = parse_as_of(as_of)
    kept: list[T] = []
    for item in items:
        raw = get_date(item)
        if raw is None:
            continue
        try:
            when = parse_as_of(raw)
        except (ValueError, TypeError):
            continue
        if when <= cutoff:
            kept.append(item)
    return kept


def assert_backtest_safe(adapter_module: str) -> None:
    """Raise if ``adapter_module`` is registered LIVE-ONLY.

    Call from any backtest/eval pipeline before consuming an adapter whose
    provenance is dynamic. Deterministic enforcement of the A3 contract —
    the check lives in code, not in a prompt.
    """
    reason = LIVE_ONLY_ADAPTERS.get(adapter_module)
    if reason is not None:
        raise RuntimeError(
            f"{adapter_module} is LIVE-ONLY and cannot honor an as-of date: "
            f"{reason}. Backtest/eval paths must not consume it (A3 contract)."
        )


def latest_knowable(
    dates: Iterable["str | _dt.date | _dt.datetime"],
    as_of: "str | _dt.date | _dt.datetime",
) -> Optional[_dt.date]:
    """Return the max date <= as_of, or None. Convenience for adapters that
    need 'the most recent observation knowable at the simulation date'."""
    cutoff = parse_as_of(as_of)
    best: Optional[_dt.date] = None
    for raw in dates:
        try:
            when = parse_as_of(raw)
        except (ValueError, TypeError):
            continue
        if when <= cutoff and (best is None or when > best):
            best = when
    return best
