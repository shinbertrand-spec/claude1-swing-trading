"""Edge-triggered push de-duplication for the entry watcher.

A trigger that stays in the same state across consecutive ticks must page ONCE,
not every hour (mirrors the observability cockpit alert dispatcher). Push only
when a ticker ESCALATES to a more-actionable status (inactive → approaching →
in_zone → fired). A `fired` condition re-pages once per ET day so a persistent
entry window doesn't go silent after the first ping. De-escalation (price moves
away) is recorded silently so the name can re-fire later.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .evaluate import RANK, STATUS_FIRED, WatchSignal

_REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_DEFAULT = _REPO_ROOT / "journal" / "watcher" / "_state.json"


def load_state(path: Path = STATE_DEFAULT) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict[str, Any], path: Path = STATE_DEFAULT) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def decide_pushes(
    signals: list[WatchSignal],
    state: dict[str, Any],
    *,
    today_key: str,
) -> tuple[list[WatchSignal], dict[str, Any]]:
    """Return (signals_to_push, updated_state).

    Push rule, per ticker:
      * escalation — new rank > last-pushed rank → push.
      * fired re-page — status is `fired` and we haven't pushed a fire today → push.
      * otherwise — record the new status, do not push (suppressed).
    """
    to_push: list[WatchSignal] = []
    new_state = dict(state)
    for s in signals:
        prev = state.get(s.ticker) or {}
        prev_rank = RANK.get(prev.get("status", "inactive"), -1)
        rank = RANK.get(s.status, 0)

        push = False
        if rank > prev_rank and rank >= RANK["approaching"]:
            push = True  # escalation into (or further up) the actionable band
        elif s.status == STATUS_FIRED and prev.get("fired_date") != today_key:
            push = True  # daily re-page while a fired window persists

        entry = {"status": s.status, "rank": rank}
        if s.status == STATUS_FIRED and push:
            entry["fired_date"] = today_key
        elif prev.get("fired_date") and s.status == STATUS_FIRED:
            entry["fired_date"] = prev["fired_date"]
        new_state[s.ticker] = entry
        if push:
            to_push.append(s)
    return to_push, new_state
