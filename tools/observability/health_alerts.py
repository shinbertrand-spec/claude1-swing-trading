"""Edge-triggered Telegram alert dispatcher for the health cockpit.

Reads a :class:`~tools.observability.health_snapshot.HealthSnapshot` and pushes
Telegram alerts for the two page-level conditions of this slice:

  A. SILENT-FAILURE — a scheduled entry session placed nothing it intended to
     (``intended>0 AND (placed==0 OR dry>0)``). The 10-day-outage signature.
  B. ERRORS / FEED-DOWN — run errors, a data feed unreachable, or the entry
     task overdue.

Edge-triggered + de-duplicated via a small state file so a condition that stays
true across consecutive runs pages ONCE, not every 60 minutes (mirrors the
kill-switch watchdog's silent/recovered pattern). Recovery messages fire when a
feed comes back. No per-veto / per-signal noise — that's a later slice.

OBSERVE-ONLY. Reuses :func:`tools.thematic_portfolio.kill_switch.telegram_alert.send_alert`
(never raises; resolves token + authorized chat from ``~/.claude/channels/telegram/``).
Messages are PII-safe: only counts, tickers (public), timestamps, and the
already-last-4-masked account. This module never raises.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .health_snapshot import HealthSnapshot

UTC = timezone.utc
_REPO_ROOT = Path(__file__).resolve().parents[2]
ALERT_STATE_DEFAULT = _REPO_ROOT / "ledgers" / "observability" / "_state" / "alert_state.json"


@dataclass
class DispatchResult:
    sent: list[str] = field(default_factory=list)       # human labels of alerts pushed
    suppressed: list[str] = field(default_factory=list)  # conditions true but de-duped
    errors: list[str] = field(default_factory=list)      # send failures (best-effort)

    def to_dict(self) -> dict[str, Any]:
        return {"sent": self.sent, "suppressed": self.suppressed, "errors": self.errors}


# ---------------------------------------------------------------------------
# message formatters (PII-safe)
# ---------------------------------------------------------------------------


def format_silent_failure(sf: Any) -> str:
    return (
        "🚨 *AUTO-PAPER SILENT FAILURE*\n"
        f"ANOMALY: {sf.intended} intended, {sf.placed} placed, "
        f"{sf.dry_run} dry-run — investigate\n"
        f"Session: `{sf.run_id}` (started {sf.session_started_iso})\n"
        "Scheduled entry run placed nothing it intended to. "
        "Check the entry task isn't stuck in --dry-run."
    )


def format_feed_down(feed: Any) -> str:
    return (
        "⚠️ *DATA FEED DOWN*\n"
        f"{feed.name}: {feed.detail}\n"
        "Health checks degraded until it recovers."
    )


def format_feed_recovered(feed: Any) -> str:
    return f"✅ *DATA FEED RECOVERED*\n{feed.name} reachable again ({feed.detail})."


def format_entry_overdue(task: Any) -> str:
    return (
        "🚨 *AUTO-PAPER ENTRY OVERDUE*\n"
        f"{task.detail}\n"
        f"Last entry run: {task.last_run_iso or 'unknown'}. "
        "The 9:35 ET entry task may have failed to fire."
    )


def format_errors(run_id: Optional[str], count: int) -> str:
    return (
        "⚠️ *AUTO-PAPER RUN ERRORS*\n"
        f"{count} error(s) recorded in run `{run_id}`. Check _status.yml errors[]."
    )


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


def _load_state(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def dispatch_alerts(
    snapshot: HealthSnapshot,
    *,
    state_path: Path = ALERT_STATE_DEFAULT,
    send: Optional[Callable[..., Any]] = None,
    now_utc: Optional[datetime] = None,
) -> DispatchResult:
    """Push edge-triggered alerts for the snapshot. Never raises."""
    result = DispatchResult()
    try:
        if send is None:
            from ..thematic_portfolio.kill_switch.telegram_alert import send_alert as send

        state = _load_state(state_path)
        now = now_utc or datetime.now(UTC)
        today_et_key = now.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date().isoformat()

        def _push(label: str, message: str) -> None:
            try:
                res = send(message)
                ok = getattr(res, "ok", False)
                if ok:
                    result.sent.append(label)
                else:
                    result.errors.append(f"{label}: {getattr(res, 'error', 'send_failed')}")
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"{label}: {exc!r}")

        # --- A. silent failure (edge: one per run_id) ---
        sf = snapshot.silent_failure
        if sf and sf.alarm:
            if state.get("silent_failure_run_id") != sf.run_id:
                _push("silent_failure", format_silent_failure(sf))
                state["silent_failure_run_id"] = sf.run_id
            else:
                result.suppressed.append("silent_failure")

        # --- B1. entry overdue (edge: one per ET day) ---
        for task in snapshot.cron_tasks:
            if task.name == "AutoPaperEntry" and task.overdue:
                if state.get("entry_overdue_date") != today_et_key:
                    _push("entry_overdue", format_entry_overdue(task))
                    state["entry_overdue_date"] = today_et_key
                else:
                    result.suppressed.append("entry_overdue")

        # --- B2. run errors (edge: one per run_id) ---
        if snapshot.error_count > 0:
            run_id = sf.run_id if sf else None
            if state.get("errors_run_id") != run_id:
                _push("run_errors", format_errors(run_id, snapshot.error_count))
                state["errors_run_id"] = run_id
            else:
                result.suppressed.append("run_errors")

        # --- B3. feed down / recovered (edge: track currently-down set) ---
        prev_down = set(state.get("feeds_down") or [])
        now_down = {f.name for f in snapshot.feeds if not f.up}
        feed_by_name = {f.name: f for f in snapshot.feeds}
        for name in sorted(now_down - prev_down):
            _push(f"feed_down:{name}", format_feed_down(feed_by_name[name]))
        for name in sorted(prev_down - now_down):
            fb = feed_by_name.get(name)
            if fb is not None:
                _push(f"feed_recovered:{name}", format_feed_recovered(fb))
        # Only persist the down-set when feeds were actually probed.
        if snapshot.feeds:
            state["feeds_down"] = sorted(now_down)
        for name in sorted(now_down & prev_down):
            result.suppressed.append(f"feed_down:{name}")

        _save_state(state_path, state)
    except Exception as exc:  # noqa: BLE001 — dispatcher must never crash the run
        result.errors.append(f"dispatch_failed: {exc!r}")
    return result
