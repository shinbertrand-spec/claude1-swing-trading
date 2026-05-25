"""US regular-session detector for Process B's sleep cadence.

Wraps :mod:`tools.market_calendar` (day-level: holidays + weekends) with an
intraday RTH (9:30-16:00 ET) check. Returns whether the kill-switch monitor
should be in **session cadence** (60s sleep) or **off-hours cadence** (300s).

The design pseudocode in [[swing-thematic-portfolio-kill-switch-architecture]]:

    sleep(60 if market_open() else 300)

This module is the implementation of ``market_open()``. No extended-hours
sessions are modelled — the kill-switch operates on RTH price discovery
because thematic-book valuations during pre-/post-market are unreliable
(thin liquidity, gappy fills). Off-hours cadence still runs; it just
sleeps longer between cycles.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

from ...market_calendar import US_MARKET_HOLIDAYS

NY_TZ = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)   # 9:30 AM ET
RTH_CLOSE = time(16, 0)  # 4:00 PM ET

SESSION_SLEEP_SECONDS = 60
OFF_HOURS_SLEEP_SECONDS = 300


@dataclass(frozen=True)
class SessionState:
    is_rth_open: bool
    reason: str  # "open" | "weekend" | "holiday:<name>" | "pre_open" | "post_close"
    now_et_iso: str
    suggested_sleep_seconds: int


def session_state(now_utc: Optional[datetime] = None) -> SessionState:
    """Return whether RTH is currently open in US/Eastern.

    Args:
        now_utc: optional injection point for tests. Defaults to
            ``datetime.now(ZoneInfo("UTC"))``.
    """
    if now_utc is None:
        now_utc = datetime.now(ZoneInfo("UTC"))
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=ZoneInfo("UTC"))

    now_et = now_utc.astimezone(NY_TZ)
    today = now_et.date()

    if now_et.weekday() >= 5:
        return SessionState(
            is_rth_open=False,
            reason="weekend",
            now_et_iso=now_et.isoformat(timespec="seconds"),
            suggested_sleep_seconds=OFF_HOURS_SLEEP_SECONDS,
        )

    if today in US_MARKET_HOLIDAYS:
        return SessionState(
            is_rth_open=False,
            reason=f"holiday:{US_MARKET_HOLIDAYS[today]}",
            now_et_iso=now_et.isoformat(timespec="seconds"),
            suggested_sleep_seconds=OFF_HOURS_SLEEP_SECONDS,
        )

    now_t = now_et.time()
    if now_t < RTH_OPEN:
        return SessionState(
            is_rth_open=False,
            reason="pre_open",
            now_et_iso=now_et.isoformat(timespec="seconds"),
            suggested_sleep_seconds=OFF_HOURS_SLEEP_SECONDS,
        )
    if now_t >= RTH_CLOSE:
        return SessionState(
            is_rth_open=False,
            reason="post_close",
            now_et_iso=now_et.isoformat(timespec="seconds"),
            suggested_sleep_seconds=OFF_HOURS_SLEEP_SECONDS,
        )

    return SessionState(
        is_rth_open=True,
        reason="open",
        now_et_iso=now_et.isoformat(timespec="seconds"),
        suggested_sleep_seconds=SESSION_SLEEP_SECONDS,
    )
