"""Phase 3 — staleness enforcement.

Per ``swing-risk-compliance-doctrine.md`` Requirement 4 (temporal context
awareness). Every ledger section carries a ``fetched_at`` (or
``computed_at``) timestamp; this module enforces the doctrine's
max-staleness table:

==========================  ====================================
Section                     Max staleness
==========================  ====================================
quote                       4 h during market hours;
                            until next market open if session=closed
fundamentals                check filing_date drift vs next_earnings_date
technical                   1 trading day (computed on or after last close)
regime                      1 trading day
catalyst                    7 days from publication
earnings_calendar           24 h (refreshed daily)
==========================  ====================================

Plus the doctrine flags ``fundamentals.next_earnings_date`` inside the
10-trading-day blackout window as an additional caution. The blackout
itself is a hard-rule check (already in :mod:`tools.earnings_calendar`);
this module only reports it as a *warning* on the ledger sweep.

Used by ``risk-and-compliance``'s verification flow: before APPROVE, run
:func:`assert_ledger_fresh` on the candidate's ledger. Any FAIL → BLOCK.

CLI: not provided here — see :mod:`tools.ledger_freshness_audit` for the
full-ledger CLI sweep.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# Section → max-staleness window in seconds for the "elapsed time" rule.
# Some sections use bespoke logic (market-hours awareness, earnings-calendar
# blackout) and are handled explicitly in :func:`check_section`.
MAX_STALENESS_SECONDS: dict[str, int | None] = {
    "quote": 4 * 3600,            # 4 hours during market hours
    "fundamentals": None,         # bespoke: filing-date + next-earnings logic
    "technical": 24 * 3600,       # ~1 trading day rounded to 24h
    "regime": 24 * 3600,
    "catalyst": 7 * 24 * 3600,    # 7 days
    "earnings_calendar": 24 * 3600,
    # ep_specific is intentionally absent — it's a derived section composed of
    # values pulled from quote / fundamentals / catalyst. Freshness of those
    # upstream sections is what matters; ep_specific carries no fetched_at.
}

# Sections whose freshness anchor is ``computed_at`` rather than ``fetched_at``.
COMPUTED_AT_SECTIONS = {"technical", "regime"}

# Sections that get an earnings-blackout warning, not a freshness failure.
EARNINGS_BLACKOUT_TRADING_DAYS = 10

ET_ZONE = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


class StalenessError(RuntimeError):
    """Raised when a ledger section has aged past its max-staleness window."""


@dataclass
class SectionFreshness:
    """Result of evaluating one section."""

    section: str
    status: str                              # "fresh" | "stale" | "missing_timestamp" | "missing_section"
    timestamp_field: str | None = None       # which field carried fetched_at
    timestamp: str | None = None             # the ISO timestamp itself
    age_seconds: int | None = None
    max_staleness_seconds: int | None = None
    market_was_open: bool | None = None      # only meaningful for `quote`
    warnings: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class LedgerFreshnessReport:
    """Result of evaluating all sections in a ledger."""

    asof_utc: str
    sections: list[SectionFreshness]
    overall: str            # "fresh" | "stale" — fresh iff no section is stale

    @property
    def is_fresh(self) -> bool:
        return self.overall == "fresh"

    @property
    def stale_sections(self) -> list[str]:
        return [s.section for s in self.sections if s.status == "stale"]


def _parse_iso(value: str | datetime) -> datetime:
    """Parse an ISO-8601 timestamp; tolerate trailing 'Z' and already-parsed
    ``datetime`` (PyYAML auto-converts ISO timestamps)."""
    if isinstance(value, datetime):
        return value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _coerce_date(value: str | date | datetime) -> date:
    """Coerce a date-like value to a ``date``. PyYAML may auto-parse to
    either ``date`` or ``datetime`` depending on the source format."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def is_market_open_at(asof: datetime) -> bool:
    """True iff the regular US equity session (M-F 9:30-16:00 ET) is open
    at ``asof``. Doesn't model exchange holidays — Phase 3 baseline."""
    et = _to_utc(asof).astimezone(ET_ZONE)
    if is_weekend(et.date()):
        return False
    return MARKET_OPEN <= et.time() < MARKET_CLOSE


def last_market_close_before(asof: datetime) -> datetime:
    """Return the UTC datetime of the most recent US equity session's
    4:00 PM ET close strictly before ``asof``. Skips weekends."""
    et = _to_utc(asof).astimezone(ET_ZONE)
    # Walk backward to a weekday whose 16:00 has already passed at the asof.
    candidate = et.replace(hour=16, minute=0, second=0, microsecond=0)
    if candidate >= et:
        candidate -= timedelta(days=1)
    while is_weekend(candidate.date()):
        candidate -= timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _quote_fresh(
    fetched_at: datetime, asof: datetime, session: str | None
) -> tuple[bool, int, dict[str, Any]]:
    """Return (fresh, age_seconds, extra_fields).

    Rules per the doctrine:
    * Market open at ``asof`` → freshness window = 4 h.
    * Market closed at ``asof`` AND session ∈ {closed, afterhours, premarket}
      → freshness is "since last regular close" (effectively until next open).
    """
    age_s = int((asof - fetched_at).total_seconds())
    market_open = is_market_open_at(asof)
    extra: dict[str, Any] = {"market_was_open": market_open, "session": session}
    if market_open:
        return age_s <= MAX_STALENESS_SECONDS["quote"], age_s, extra
    # Outside market hours: fresh iff fetched_at is at or after the most
    # recent regular close. This way Friday 4 PM quote is fresh all weekend.
    last_close = last_market_close_before(asof)
    fresh = fetched_at >= last_close
    extra["last_close_utc"] = last_close.isoformat()
    return fresh, age_s, extra


def _trading_days_between(today: date, target: date) -> int:
    if target < today:
        return -(_business_days(target, today) - 1)
    return _business_days(today, target)


def _business_days(start: date, end: date) -> int:
    """Count business days between start (inclusive) and end (exclusive)."""
    if end <= start:
        return 0
    days = 0
    d = start
    while d < end:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


def _fundamentals_check(
    section: dict, asof: datetime
) -> tuple[str, list[str], int | None, str | None]:
    """Return (status, warnings, age_seconds, timestamp).

    Fundamentals don't expire on a clock — they expire when new earnings
    are filed. Check:
    * ``fetched_at`` present (else missing_timestamp)
    * ``filing_date`` reasonable (warn if more than 110 days old — past the
      ~90-day earnings cadence)
    * ``next_earnings_date`` within EARNINGS_BLACKOUT_TRADING_DAYS → warn
    """
    warnings: list[str] = []
    fetched_at = section.get("fetched_at")
    if not fetched_at:
        return "missing_timestamp", warnings, None, None
    fetched_dt = _to_utc(_parse_iso(fetched_at))
    age_s = int((asof - fetched_dt).total_seconds())

    filing_date_raw = section.get("filing_date")
    if filing_date_raw:
        try:
            filing_date = _coerce_date(filing_date_raw)
            days_since_filing = (asof.date() - filing_date).days
            if days_since_filing > 110:
                warnings.append(
                    f"filing_date {filing_date.isoformat()} is {days_since_filing}d old "
                    "— new earnings may have been filed since"
                )
        except (ValueError, TypeError):
            warnings.append(f"unparseable filing_date {filing_date_raw!r}")

    next_earnings_raw = section.get("next_earnings_date")
    if next_earnings_raw:
        try:
            next_earnings = _coerce_date(next_earnings_raw)
            tdays = _trading_days_between(asof.date(), next_earnings)
            if 0 <= tdays <= EARNINGS_BLACKOUT_TRADING_DAYS:
                warnings.append(
                    f"next_earnings_date {next_earnings.isoformat()} is {tdays} trading days "
                    "away — CLAUDE.md hard rule: no entries within 10 trading days "
                    "(unless EP setup)"
                )
        except (ValueError, TypeError):
            warnings.append(f"unparseable next_earnings_date {next_earnings_raw!r}")

    if not section.get("next_earnings_source_secondary"):
        warnings.append(
            "next_earnings_source_secondary missing — trade-researcher principle 1 "
            "requires verification against TWO independent sources"
        )

    return "fresh", warnings, age_s, fetched_at


def check_section(
    ledger: dict,
    section: str,
    asof: datetime | None = None,
) -> SectionFreshness:
    """Evaluate freshness of one ledger section.

    Args:
        ledger: parsed YAML ledger as a dict.
        section: top-level section key (e.g. ``"quote"``, ``"technical"``).
        asof: evaluation time; defaults to now UTC.
    """
    asof = asof or _now_utc()
    asof = _to_utc(asof)

    if section not in ledger or ledger[section] is None:
        return SectionFreshness(section=section, status="missing_section")

    sec = ledger[section]

    if section == "fundamentals":
        status, warnings, age_s, ts = _fundamentals_check(sec, asof)
        return SectionFreshness(
            section=section,
            status=status,
            timestamp_field="fetched_at" if ts else None,
            timestamp=ts,
            age_seconds=age_s,
            max_staleness_seconds=None,
            warnings=warnings,
            detail=(
                "fundamentals freshness is anchored to earnings cadence; warnings list "
                "explicit drift signals."
            ),
        )

    # Pick timestamp field by section convention.
    ts_field = "computed_at" if section in COMPUTED_AT_SECTIONS else "fetched_at"
    ts = sec.get(ts_field)
    if not ts:
        return SectionFreshness(
            section=section,
            status="missing_timestamp",
            timestamp_field=ts_field,
            detail=f"section present but {ts_field!r} not populated",
        )

    fetched_dt = _to_utc(_parse_iso(ts))
    age_s = int((asof - fetched_dt).total_seconds())
    max_s = MAX_STALENESS_SECONDS.get(section)

    if section == "quote":
        fresh, age_s, extra = _quote_fresh(fetched_dt, asof, sec.get("session"))
        return SectionFreshness(
            section=section,
            status="fresh" if fresh else "stale",
            timestamp_field=ts_field,
            timestamp=ts,
            age_seconds=age_s,
            max_staleness_seconds=max_s,
            market_was_open=extra["market_was_open"],
            detail=(
                f"market_open={extra['market_was_open']} session={extra.get('session')} "
                f"last_close={extra.get('last_close_utc', 'n/a')}"
            ),
        )

    if max_s is None:
        return SectionFreshness(
            section=section,
            status="fresh",
            timestamp_field=ts_field,
            timestamp=ts,
            age_seconds=age_s,
            detail="no clock-based staleness rule for this section",
        )

    return SectionFreshness(
        section=section,
        status="fresh" if age_s <= max_s else "stale",
        timestamp_field=ts_field,
        timestamp=ts,
        age_seconds=age_s,
        max_staleness_seconds=max_s,
    )


def audit_ledger(
    ledger: dict,
    sections: list[str] | None = None,
    asof: datetime | None = None,
) -> LedgerFreshnessReport:
    """Run :func:`check_section` over a set of sections.

    Args:
        ledger: parsed YAML ledger as a dict.
        sections: subset to evaluate. Default = every section in
            :data:`MAX_STALENESS_SECONDS` that is present in the ledger.
        asof: evaluation time; defaults to now UTC.
    """
    asof = asof or _now_utc()
    asof = _to_utc(asof)
    if sections is None:
        sections = [s for s in MAX_STALENESS_SECONDS if s in ledger]
    results = [check_section(ledger, s, asof=asof) for s in sections]
    overall = "fresh" if all(r.status != "stale" for r in results) else "stale"
    return LedgerFreshnessReport(
        asof_utc=asof.isoformat(timespec="seconds"),
        sections=results,
        overall=overall,
    )


def assert_ledger_fresh(
    ledger: dict,
    sections: list[str] | None = None,
    asof: datetime | None = None,
) -> LedgerFreshnessReport:
    """Wrapper that raises :class:`StalenessError` if any section is stale.

    Returns the report on success (so callers can still inspect warnings).
    """
    report = audit_ledger(ledger, sections=sections, asof=asof)
    if not report.is_fresh:
        details = "; ".join(
            f"{r.section}({r.detail or 'age_s=' + str(r.age_seconds)})"
            for r in report.sections
            if r.status == "stale"
        )
        raise StalenessError(
            f"ledger stale sections: {report.stale_sections}. Details: {details}"
        )
    return report
