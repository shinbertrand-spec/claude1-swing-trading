"""State files for the kill-switch monitor (Process B).

Four files at ``ledgers/thematic/kill_switch/_state/`` (gitignored):

* **peak.json** — rolling thematic-book peak USD value + last-updated.
  Peak is monotone-non-decreasing within a "monitoring epoch"; a full
  recovery + reasoning-layer re-engagement resets the epoch.
* **heartbeat.json** — last-cycle timestamp + cycle number + dry-run flag.
  Tells the watchdog (Session 2) that Process B is alive.
* **aschenbrenner_kill_event.json** — boolean flag + metadata. Written
  by the artifact classifier (Session 1 follow-up) when thesis-abandonment
  or SA LP closure signals are detected. Read by the kill-switch monitor
  every cycle. Once set, requires explicit human clearance to reset
  (the design says false-positive cost is preferred over false-negative).
* **events.jsonl** — append-only event log. One JSON object per cycle,
  including ``action=hold`` cycles, so the log doubles as a heartbeat
  trail. Order placements (Session 2) reference the event by ``cycle_id``.

All four files are co-located under ``_state/`` so a single
``rm -rf ledgers/thematic/kill_switch/_state/`` resets the monitor
cleanly (used by tests + epoch resets after a tier-3 unwind).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_STATE_DIR = Path("ledgers/thematic/kill_switch/_state")
PEAK_FILENAME = "peak.json"
HEARTBEAT_FILENAME = "heartbeat.json"
KILL_EVENT_FILENAME = "aschenbrenner_kill_event.json"
EVENTS_FILENAME = "events.jsonl"

PEAK_SCHEMA_VERSION = "1.0"
HEARTBEAT_SCHEMA_VERSION = "1.0"
KILL_EVENT_SCHEMA_VERSION = "1.0"
EVENT_SCHEMA_VERSION = "1.0"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# peak.json
# ---------------------------------------------------------------------------


@dataclass
class PeakState:
    """Rolling peak thematic-book USD value."""

    peak_value: float
    updated_at: str
    epoch_started_at: str
    schema_version: str = PEAK_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def peak_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or DEFAULT_STATE_DIR) / PEAK_FILENAME


def load_peak(state_dir: Optional[Path] = None) -> Optional[PeakState]:
    """Return the saved peak state, or None if no peak has been recorded."""
    p = peak_path(state_dir)
    if not p.exists():
        return None
    doc = json.loads(p.read_text(encoding="utf-8"))
    return PeakState(
        peak_value=float(doc["peak_value"]),
        updated_at=str(doc["updated_at"]),
        epoch_started_at=str(doc["epoch_started_at"]),
        schema_version=str(doc.get("schema_version", PEAK_SCHEMA_VERSION)),
    )


def save_peak(peak: PeakState, state_dir: Optional[Path] = None) -> None:
    p = peak_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(peak.to_dict(), indent=2), encoding="utf-8")


def update_peak(
    current_thematic_value: float,
    state_dir: Optional[Path] = None,
    *,
    now_iso: Optional[str] = None,
) -> PeakState:
    """Update the peak if current > stored. Initialise on first call.

    Returns the resulting PeakState (saved to disk).
    """
    ts = now_iso or _utc_now_iso()
    existing = load_peak(state_dir)
    if existing is None:
        peak = PeakState(
            peak_value=max(0.0, float(current_thematic_value)),
            updated_at=ts,
            epoch_started_at=ts,
        )
        save_peak(peak, state_dir)
        return peak
    if current_thematic_value > existing.peak_value:
        peak = PeakState(
            peak_value=float(current_thematic_value),
            updated_at=ts,
            epoch_started_at=existing.epoch_started_at,
        )
        save_peak(peak, state_dir)
        return peak
    return existing


def reset_epoch(
    new_peak_value: float,
    state_dir: Optional[Path] = None,
    *,
    now_iso: Optional[str] = None,
) -> PeakState:
    """Force a new monitoring epoch with ``new_peak_value`` as the seed.

    Used after a tier-3 unwind + reasoning-layer re-engagement (Loop 5
    phasing back up). Process A is responsible for calling this — the
    monitor does NOT reset itself.
    """
    ts = now_iso or _utc_now_iso()
    peak = PeakState(
        peak_value=max(0.0, float(new_peak_value)),
        updated_at=ts,
        epoch_started_at=ts,
    )
    save_peak(peak, state_dir)
    return peak


# ---------------------------------------------------------------------------
# heartbeat.json
# ---------------------------------------------------------------------------


@dataclass
class HeartbeatState:
    """Last-cycle metadata. Tells the watchdog Process B is alive."""

    last_cycle_at: str
    cycle_number: int
    dry_run: bool
    last_action: str  # "hold" | "deleverage" | "unwind"
    last_tier: int  # 0 | 1 | 2 | 3
    schema_version: str = HEARTBEAT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def heartbeat_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or DEFAULT_STATE_DIR) / HEARTBEAT_FILENAME


def load_heartbeat(state_dir: Optional[Path] = None) -> Optional[HeartbeatState]:
    p = heartbeat_path(state_dir)
    if not p.exists():
        return None
    doc = json.loads(p.read_text(encoding="utf-8"))
    return HeartbeatState(
        last_cycle_at=str(doc["last_cycle_at"]),
        cycle_number=int(doc["cycle_number"]),
        dry_run=bool(doc["dry_run"]),
        last_action=str(doc["last_action"]),
        last_tier=int(doc["last_tier"]),
        schema_version=str(doc.get("schema_version", HEARTBEAT_SCHEMA_VERSION)),
    )


def save_heartbeat(
    hb: HeartbeatState, state_dir: Optional[Path] = None
) -> None:
    p = heartbeat_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(hb.to_dict(), indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# aschenbrenner_kill_event.json
# ---------------------------------------------------------------------------


@dataclass
class KillEventFlag:
    """Aschenbrenner-kill-event flag — set by the artifact classifier."""

    fired: bool
    fired_at: Optional[str] = None
    signal_type: Optional[str] = None  # "thesis_abandonment" | "sa_lp_event" | ...
    matched_phrase: Optional[str] = None
    source_artifact_url: Optional[str] = None
    notes: Optional[str] = None
    schema_version: str = KILL_EVENT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def kill_event_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or DEFAULT_STATE_DIR) / KILL_EVENT_FILENAME


def load_kill_event(state_dir: Optional[Path] = None) -> KillEventFlag:
    """Return the kill-event flag. Defaults to ``fired=False`` if file missing."""
    p = kill_event_path(state_dir)
    if not p.exists():
        return KillEventFlag(fired=False)
    doc = json.loads(p.read_text(encoding="utf-8"))
    return KillEventFlag(
        fired=bool(doc.get("fired", False)),
        fired_at=doc.get("fired_at"),
        signal_type=doc.get("signal_type"),
        matched_phrase=doc.get("matched_phrase"),
        source_artifact_url=doc.get("source_artifact_url"),
        notes=doc.get("notes"),
        schema_version=str(doc.get("schema_version", KILL_EVENT_SCHEMA_VERSION)),
    )


def set_kill_event(
    signal_type: str,
    matched_phrase: str,
    source_artifact_url: Optional[str] = None,
    notes: Optional[str] = None,
    state_dir: Optional[Path] = None,
    *,
    now_iso: Optional[str] = None,
) -> KillEventFlag:
    """Set the kill-event flag to True. Idempotent — preserves the original
    fired_at if already set (first-occurrence wins for forensics)."""
    ts = now_iso or _utc_now_iso()
    existing = load_kill_event(state_dir)
    if existing.fired:
        return existing  # do not overwrite first-fire metadata
    flag = KillEventFlag(
        fired=True,
        fired_at=ts,
        signal_type=signal_type,
        matched_phrase=matched_phrase,
        source_artifact_url=source_artifact_url,
        notes=notes,
    )
    p = kill_event_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(flag.to_dict(), indent=2), encoding="utf-8")
    return flag


def clear_kill_event(
    state_dir: Optional[Path] = None,
    *,
    cleared_by: str = "manual",
    cleared_at: Optional[str] = None,
) -> None:
    """Clear the kill-event flag. **Requires explicit human clearance** —
    the monitor never calls this on its own. Used after Bertrand reviews
    a false-positive flag, or after a full tier-3 unwind + re-engagement
    decision via Process A.

    Writes a ``cleared_*.json`` archive next to ``_state/`` for audit.
    """
    sd = state_dir or DEFAULT_STATE_DIR
    p = kill_event_path(state_dir)
    if not p.exists():
        return
    existing_doc = json.loads(p.read_text(encoding="utf-8"))
    archive = {
        "originally": existing_doc,
        "cleared_by": cleared_by,
        "cleared_at": cleared_at or _utc_now_iso(),
    }
    archive_path = sd / f"aschenbrenner_kill_event_cleared_{int(datetime.now(timezone.utc).timestamp())}.json"
    sd.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(json.dumps(archive, indent=2), encoding="utf-8")
    p.unlink()


# ---------------------------------------------------------------------------
# events.jsonl
# ---------------------------------------------------------------------------


@dataclass
class CycleEvent:
    """One cycle event in the append-only log."""

    cycle_id: str
    cycle_number: int
    fired_at: str
    dry_run: bool
    action: str  # "hold" | "deleverage" | "unwind"
    tier: int  # 0 | 1 | 2 | 3
    drawdown_pct: float
    current_allocation_pct: float
    target_allocation_pct: float
    sell_fraction: float
    thematic_market_value: float
    peak_thematic_value: float
    total_account_value: float
    aschenbrenner_kill_event: bool
    aschenbrenner_override: bool
    rationale: str
    thematic_symbols: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    orders_placed: list[dict[str, Any]] = field(default_factory=list)
    schema_version: str = EVENT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def events_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or DEFAULT_STATE_DIR) / EVENTS_FILENAME


def append_event(event: CycleEvent, state_dir: Optional[Path] = None) -> None:
    """Append-only write of a cycle event. Creates parent dirs as needed."""
    p = events_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event.to_dict(), separators=(",", ":")) + "\n"
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line)


def read_events(
    state_dir: Optional[Path] = None,
    *,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Read the event log. Returns newest-first when limit is set, else
    full chronological."""
    p = events_path(state_dir)
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    if limit is not None:
        return list(reversed(events[-limit:]))
    return events


def most_recent_fired_tier(state_dir: Optional[Path] = None) -> int:
    """Walk events.jsonl backwards; return the most recent non-zero tier
    (i.e., the most recent actual fire). Returns 0 if none."""
    p = events_path(state_dir)
    if not p.exists():
        return 0
    lines = p.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        doc = json.loads(line)
        tier = int(doc.get("tier", 0))
        if tier > 0:
            return tier
    return 0
