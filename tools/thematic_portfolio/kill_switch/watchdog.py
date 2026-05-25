"""Process C — kill-switch heartbeat watchdog.

Reads Process B's ``heartbeat.json`` every cycle (5 min cadence via cron).
If B is silent past the RTH or off-hours threshold, sets the
``kill_switch_unavailable.json`` flag that Process A (the thematic-portfolio
orchestrator) reads before recommending new positions. Per
[[swing-thematic-portfolio-kill-switch-architecture]] § Heartbeat
monitoring:

    a 3rd lightweight process pings both A and B every 5 minutes; if
    either is silent for 15 min, alert Bertrand. If B is silent,
    **default to "kill switch is firing"** and disable any A-side new
    orders until B is restored.

**Process A pinging is deferred** in v1 — the orchestrator is event-
driven (manual `/thematic-portfolio` invocation), not a daemon, so
"silent" doesn't have a natural meaning there yet. Once Process A is
daemonised, extend this module.

## State files

* ``kill_switch_unavailable.json`` — written/cleared by the watchdog;
  read by the orchestrator via :func:`is_kill_switch_unavailable`.
  Schema::

      {
        "unavailable": true,
        "since": "<iso>",
        "reason": "process_b_silent" | "heartbeat_missing",
        "last_heartbeat_at": "<iso>" | null,
        "minutes_silent": <float>,
        "schema_version": "1.0"
      }

* ``watchdog.json`` — watchdog's own state (last check, last alert sent,
  current verdict). Append-only would be overkill — single-document
  rewrites are fine for a cron-driven observer.

## Side effects on transitions

* B silent -> set flag + Telegram alert.
* B recovered (transition silent -> fresh) -> clear flag + Telegram alert.
* Steady-state silent (already flagged, still silent) -> no new alert
  (avoid spam; one alert per silence episode).
* Steady-state fresh (no flag, still fresh) -> no alert.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import state, telegram_alert
from .clock import session_state

# Silence thresholds. Per the design memo: 15 min during RTH, with a
# proportional relax off-hours. Choosing 30 min off-hours = 2x RTH.
RTH_SILENCE_THRESHOLD_MINUTES = 15
OFF_HOURS_SILENCE_THRESHOLD_MINUTES = 30

KILL_SWITCH_UNAVAILABLE_FILENAME = "kill_switch_unavailable.json"
WATCHDOG_STATE_FILENAME = "watchdog.json"

UNAVAILABLE_SCHEMA_VERSION = "1.0"
WATCHDOG_SCHEMA_VERSION = "1.0"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat(timespec="seconds")


@dataclass
class WatchdogState:
    """Watchdog's own state — last check + last verdict."""

    last_check_at: str
    cycle_number: int
    last_verdict: str  # "fresh" | "silent" | "missing"
    flag_set: bool
    schema_version: str = WATCHDOG_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UnavailableFlag:
    """The flag Process A reads to learn 'kill-switch is unavailable'."""

    unavailable: bool
    since: Optional[str] = None
    reason: Optional[str] = None  # "process_b_silent" | "heartbeat_missing"
    last_heartbeat_at: Optional[str] = None
    minutes_silent: Optional[float] = None
    schema_version: str = UNAVAILABLE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WatchdogCycleResult:
    """In-memory summary of one watchdog cycle."""

    verdict: str  # "fresh" | "silent" | "missing"
    threshold_minutes: int
    minutes_silent: Optional[float]
    flag_now_set: bool
    transition: Optional[str]  # "silent->fresh" | "fresh->silent" | None
    alert_sent: bool
    last_heartbeat_at: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def unavailable_flag_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or state.DEFAULT_STATE_DIR) / KILL_SWITCH_UNAVAILABLE_FILENAME


def watchdog_state_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or state.DEFAULT_STATE_DIR) / WATCHDOG_STATE_FILENAME


def load_unavailable_flag(state_dir: Optional[Path] = None) -> UnavailableFlag:
    """Return the unavailable flag. Defaults to ``unavailable=False`` if absent."""
    p = unavailable_flag_path(state_dir)
    if not p.exists():
        return UnavailableFlag(unavailable=False)
    doc = json.loads(p.read_text(encoding="utf-8"))
    return UnavailableFlag(
        unavailable=bool(doc.get("unavailable", False)),
        since=doc.get("since"),
        reason=doc.get("reason"),
        last_heartbeat_at=doc.get("last_heartbeat_at"),
        minutes_silent=doc.get("minutes_silent"),
        schema_version=str(doc.get("schema_version", UNAVAILABLE_SCHEMA_VERSION)),
    )


def _save_unavailable_flag(
    flag: UnavailableFlag, state_dir: Optional[Path] = None,
) -> None:
    p = unavailable_flag_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(flag.to_dict(), indent=2), encoding="utf-8")


def _clear_unavailable_flag(state_dir: Optional[Path] = None) -> None:
    p = unavailable_flag_path(state_dir)
    if p.exists():
        p.unlink()


def load_watchdog_state(state_dir: Optional[Path] = None) -> Optional[WatchdogState]:
    p = watchdog_state_path(state_dir)
    if not p.exists():
        return None
    doc = json.loads(p.read_text(encoding="utf-8"))
    return WatchdogState(
        last_check_at=str(doc["last_check_at"]),
        cycle_number=int(doc["cycle_number"]),
        last_verdict=str(doc["last_verdict"]),
        flag_set=bool(doc["flag_set"]),
        schema_version=str(doc.get("schema_version", WATCHDOG_SCHEMA_VERSION)),
    )


def _save_watchdog_state(
    s: WatchdogState, state_dir: Optional[Path] = None,
) -> None:
    p = watchdog_state_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s.to_dict(), indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public API for Process A (orchestrator) flag-check
# ---------------------------------------------------------------------------


def is_kill_switch_unavailable(state_dir: Optional[Path] = None) -> bool:
    """Return whether Process A should pause new orders.

    Reads :data:`KILL_SWITCH_UNAVAILABLE_FILENAME`; returns False when
    absent. Process A (the ``/thematic-portfolio`` orchestrator) calls
    this before each Loop 1 firing — if True, refuse to recommend new
    positions (existing positions are unaffected; only the recommend
    path is gated).
    """
    return load_unavailable_flag(state_dir).unavailable


# ---------------------------------------------------------------------------
# Watchdog cycle
# ---------------------------------------------------------------------------


def _parse_iso(s: str) -> datetime:
    cleaned = s.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    return datetime.fromisoformat(cleaned)


def _minutes_since(iso_ts: str, now: datetime) -> float:
    return (now - _parse_iso(iso_ts)).total_seconds() / 60.0


def cycle(
    *,
    state_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    alert_fn: Any = None,
    cycle_number: int = 0,
) -> WatchdogCycleResult:
    """Run one watchdog cycle.

    Args:
        state_dir: override the state directory (used in tests).
        now: injection point for the current UTC time (used in tests).
        alert_fn: injection point for the alert send function. Default
            is :func:`telegram_alert.send_alert`. Best-effort — alert
            failures do not change the verdict.
        cycle_number: monotonically-increasing cycle counter for the
            watchdog's own state.

    Returns a :class:`WatchdogCycleResult` for inspection.
    """
    now = now or _utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    clock = session_state(now)
    threshold = (
        RTH_SILENCE_THRESHOLD_MINUTES if clock.is_rth_open
        else OFF_HOURS_SILENCE_THRESHOLD_MINUTES
    )

    hb = state.load_heartbeat(state_dir)
    prior_watchdog = load_watchdog_state(state_dir)
    prior_flag = load_unavailable_flag(state_dir)

    # Verdict
    verdict: str
    minutes_silent: Optional[float] = None
    last_hb_iso: Optional[str] = None
    if hb is None:
        verdict = "missing"
        # "Missing" is treated the same as silent for flag purposes,
        # but with a distinct reason so the orchestrator can tell.
    else:
        last_hb_iso = hb.last_cycle_at
        minutes_silent = _minutes_since(hb.last_cycle_at, now)
        verdict = "silent" if minutes_silent > threshold else "fresh"

    flag_should_be_set = verdict in ("silent", "missing")

    # Transition detection.
    prior_verdict = prior_watchdog.last_verdict if prior_watchdog else None
    transition: Optional[str] = None
    if prior_verdict in ("silent", "missing") and verdict == "fresh":
        transition = "silent->fresh"
    elif prior_verdict in (None, "fresh") and verdict in ("silent", "missing"):
        transition = "fresh->silent"

    # Side effects.
    alert_sent = False
    send = alert_fn or telegram_alert.send_alert

    if flag_should_be_set and not prior_flag.unavailable:
        # New silence episode — write flag + alert.
        flag = UnavailableFlag(
            unavailable=True,
            since=now.isoformat(timespec="seconds"),
            reason=(
                "heartbeat_missing" if verdict == "missing"
                else "process_b_silent"
            ),
            last_heartbeat_at=last_hb_iso,
            minutes_silent=minutes_silent,
        )
        _save_unavailable_flag(flag, state_dir)
        try:
            send(telegram_alert.format_b_silent(
                last_cycle_at=last_hb_iso,
                minutes_silent=minutes_silent or 0.0,
            ))
            alert_sent = True
        except Exception:  # noqa: BLE001
            pass
    elif not flag_should_be_set and prior_flag.unavailable:
        # Recovered — clear flag + alert.
        prior_silent_minutes = float(prior_flag.minutes_silent or 0.0)
        _clear_unavailable_flag(state_dir)
        try:
            send(telegram_alert.format_b_recovered(
                last_cycle_at=last_hb_iso or "",
                silent_minutes=prior_silent_minutes,
            ))
            alert_sent = True
        except Exception:  # noqa: BLE001
            pass
    # else: steady state (silent->silent OR fresh->fresh) — no side effects.

    # Save watchdog's own state.
    _save_watchdog_state(
        WatchdogState(
            last_check_at=now.isoformat(timespec="seconds"),
            cycle_number=cycle_number,
            last_verdict=verdict,
            flag_set=flag_should_be_set,
        ),
        state_dir=state_dir,
    )

    return WatchdogCycleResult(
        verdict=verdict,
        threshold_minutes=threshold,
        minutes_silent=minutes_silent,
        flag_now_set=flag_should_be_set,
        transition=transition,
        alert_sent=alert_sent,
        last_heartbeat_at=last_hb_iso,
    )


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.kill_switch.watchdog",
        description=(
            "Process C — kill-switch heartbeat watchdog. Reads Process B's "
            "heartbeat.json; sets/clears kill_switch_unavailable.json + pushes "
            "Telegram alerts on state transitions."
        ),
    )
    parser.add_argument("--state-dir", type=Path, default=None)
    args = parser.parse_args()

    result = cycle(state_dir=args.state_dir, cycle_number=1)
    print(json.dumps(result.to_dict(), indent=2, default=str))


if __name__ == "__main__":
    main()
