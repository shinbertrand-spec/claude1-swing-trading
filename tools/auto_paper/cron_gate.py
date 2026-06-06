"""File-based cron gate for the paper-auto track.

When the post-RTH reconciler discovers a Mode-B orphan (broker holds a position
with NO ledger), it sets this gate. The entry pipeline (``run_entry`` phase init)
refuses to scan / place while the gate is set, so the bot does not pile new
trades on top of an unknown, unreconciled broker state. The operator clears the
gate (``clear_gate`` / deleting the file) after reconciling the orphan.

Single-file JSON at ``journal/paper-auto/cron_gate.json`` (gitignored local
state, same convention as the rest of the paper-auto runtime artifacts).

This is deliberately dumb: presence of the file == gated. No expiry, no
auto-clear -- acknowledgement is a human action by design (the whole point is to
halt autonomous placement until a human looks).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any, Optional

GATE_PATH = "journal/paper-auto/cron_gate.json"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def set_gate(reason: str, payload: Optional[dict[str, Any]] = None,
             *, path: Optional[str] = None) -> str:
    """Set the gate. Idempotent: preserves the original ``since`` if already set
    (so repeated reconciler runs don't reset the clock) but refreshes the
    payload. Returns the path written."""
    p = path or GATE_PATH
    since = _now_iso()
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as fh:
                since = (json.load(fh) or {}).get("since", since)
        except (json.JSONDecodeError, OSError):
            pass
    doc = {
        "gated": True,
        "reason": reason,
        "since": since,
        "updated": _now_iso(),
        "payload": payload or {},
    }
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    return p


def is_gated(path: Optional[str] = None) -> tuple[bool, Optional[dict[str, Any]]]:
    """Return ``(gated, doc)``. ``gated`` is True iff the gate file exists and
    has ``gated: true``. A malformed gate file is treated as gated (fail-safe):
    if we can't read it, we don't place."""
    p = path or GATE_PATH
    if not os.path.isfile(p):
        return False, None
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return True, {"gated": True, "reason": "unreadable gate file (fail-safe)"}
    return bool(doc.get("gated", True)), doc


def clear_gate(path: Optional[str] = None) -> bool:
    """Clear the gate (operator acknowledgement). Returns True if a gate was
    removed, False if none was set."""
    p = path or GATE_PATH
    if os.path.isfile(p):
        os.remove(p)
        return True
    return False
