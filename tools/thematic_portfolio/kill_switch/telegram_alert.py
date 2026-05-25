"""Best-effort Telegram push for kill-switch alerts.

Python equivalent of ``scripts/send-to-telegram.ps1``, callable directly
from Process B (monitor.cycle) and Process C (watchdog) so they can push
alerts without spawning a PowerShell subprocess.

## Auth resolution (matches send-to-telegram.ps1 contract)

1. ``TELEGRAM_BOT_TOKEN`` env var (preferred for tests / explicit override)
2. ``TELEGRAM_BOT_TOKEN`` key in ``~/.claude/channels/telegram/.env``
3. ``TELEGRAM_CHAT_ID`` env var
4. First numeric entry in ``allowFrom[]`` of
   ``~/.claude/channels/telegram/access.json``

If either token or chat_id cannot be resolved, ``send_alert`` returns a
result with ``ok=False`` and ``error="config_missing"`` — it does NOT
raise. Process B must never crash on an alert-config issue.

## Network failure semantics

Network errors are caught and returned as ``ok=False`` with the
exception message. Process B's cycle continues; the missed alert
becomes a watchdog concern (Process C is the one that flags "B silent").

## No PII / no secrets

Per CLAUDE.md § Sensitive Information: alert messages never include
tokens, account numbers, or full position cost-basis. Only the
kill-switch's tier / sell_fraction / drawdown / symbol-list (which is
already public information about thematic-book composition once Loop 1
has fired).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import urllib.error
import urllib.request

DEFAULT_ENV_PATH = Path.home() / ".claude" / "channels" / "telegram" / ".env"
DEFAULT_ACCESS_PATH = Path.home() / ".claude" / "channels" / "telegram" / "access.json"

TELEGRAM_API_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"
DEFAULT_TIMEOUT_SECONDS = 5.0


@dataclass
class AlertResult:
    """Outcome of one :func:`send_alert` call."""

    ok: bool
    chat_id: Optional[str] = None
    message_id: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _read_token_from_env_file(env_path: Path) -> Optional[str]:
    if not env_path.exists():
        return None
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "TELEGRAM_BOT_TOKEN":
                return value.strip().strip("'\"")
    except OSError:
        return None
    return None


def _read_chat_id_from_access_file(access_path: Path) -> Optional[str]:
    if not access_path.exists():
        return None
    try:
        doc = json.loads(access_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    allow_from = doc.get("allowFrom") or []
    for entry in allow_from:
        # Tolerate either ["123", ...] or [{"id": "123"}, ...]
        if isinstance(entry, (int, str)):
            s = str(entry).strip()
            if s:
                return s
        elif isinstance(entry, dict):
            v = entry.get("id") or entry.get("chat_id")
            if v is not None:
                return str(v).strip()
    return None


def resolve_credentials(
    *,
    env_path: Optional[Path] = None,
    access_path: Optional[Path] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve ``(bot_token, chat_id)`` from env vars + on-disk config.

    Returns ``(None, None)`` if either piece is missing.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        token = _read_token_from_env_file(env_path or DEFAULT_ENV_PATH)

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        chat_id = _read_chat_id_from_access_file(access_path or DEFAULT_ACCESS_PATH)

    return token, chat_id


def send_alert(
    message: str,
    *,
    parse_mode: str = "Markdown",
    env_path: Optional[Path] = None,
    access_path: Optional[Path] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    _post_fn=None,  # injected for tests
) -> AlertResult:
    """Best-effort push to Telegram. **Never raises.**

    Returns :class:`AlertResult` with ``ok=False`` when config is missing
    or the POST fails. Caller can log + continue.
    """
    if not message or not message.strip():
        return AlertResult(ok=False, error="empty_message")

    token, chat_id = resolve_credentials(
        env_path=env_path, access_path=access_path,
    )
    if not token:
        return AlertResult(ok=False, error="config_missing_token")
    if not chat_id:
        return AlertResult(ok=False, error="config_missing_chat_id")

    url = TELEGRAM_API_TEMPLATE.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": parse_mode,
    }
    body = json.dumps(payload).encode("utf-8")

    if _post_fn is not None:
        # Test seam — caller injects a fake POST.
        try:
            resp = _post_fn(url=url, body=body, timeout=timeout)
            return _interpret_response(resp, chat_id)
        except Exception as exc:  # noqa: BLE001
            return AlertResult(ok=False, chat_id=chat_id, error=f"post_failed: {exc}")

    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        doc = json.loads(raw)
        return _interpret_response(doc, chat_id)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return AlertResult(ok=False, chat_id=chat_id, error=f"post_failed: {exc}")


def _interpret_response(resp: dict, chat_id: str) -> AlertResult:
    if not isinstance(resp, dict):
        return AlertResult(ok=False, chat_id=chat_id, error="bad_response_shape")
    if resp.get("ok") is True:
        result = resp.get("result") or {}
        return AlertResult(
            ok=True, chat_id=chat_id,
            message_id=int(result.get("message_id")) if result.get("message_id") else None,
        )
    return AlertResult(
        ok=False, chat_id=chat_id,
        error=f"telegram_api_error: {resp.get('description', 'unknown')}",
    )


# ---------------------------------------------------------------------------
# Convenience formatters for kill-switch messages
# ---------------------------------------------------------------------------


def format_tier_fire(
    *,
    tier: int,
    action: str,
    drawdown_pct: float,
    current_allocation_pct: float,
    sell_fraction: float,
    thematic_symbols: list[str],
    aschenbrenner_override: bool,
    cycle_id: str,
    dry_run: bool,
) -> str:
    """Format a tier-fire alert (Markdown). Suitable for direct send_alert."""
    severity = {1: "WARNING", 2: "HIGH", 3: "URGENT"}.get(tier, "INFO")
    dry_run_prefix = "[DRY-RUN] " if dry_run else ""
    override_note = ""
    if aschenbrenner_override:
        override_note = (
            "\n*Reason:* Aschenbrenner kill-event flag set "
            "(thesis-abandonment / SA LP closure / regulatory / principal incident)."
        )
    symbols_str = ", ".join(thematic_symbols) if thematic_symbols else "(none)"
    return (
        f"{dry_run_prefix}*KILL-SWITCH TIER {tier} {severity}*\n"
        f"Action: `{action}`\n"
        f"Drawdown: {drawdown_pct:.1%}\n"
        f"Current allocation: {current_allocation_pct:.1%}\n"
        f"Sell fraction: {sell_fraction:.1%}\n"
        f"Thematic symbols: {symbols_str}\n"
        f"Cycle: `{cycle_id}`"
        f"{override_note}"
    )


def format_unprotected(*, symbols: list[str], cycle_id: str) -> str:
    return (
        "*KILL-SWITCH UNPROTECTED STATE*\n"
        "Cancel succeeded but place-limit-sell failed for: "
        f"{', '.join(symbols)}\n"
        f"Cycle: `{cycle_id}` — next cycle will retry."
    )


def format_b_silent(*, last_cycle_at: Optional[str], minutes_silent: float) -> str:
    last_str = last_cycle_at or "never"
    return (
        "*KILL-SWITCH WATCHDOG: Process B SILENT*\n"
        f"Last heartbeat: {last_str}\n"
        f"Minutes silent: {minutes_silent:.1f}\n"
        "Defaulting to 'kill-switch unavailable' — A-side new orders should be paused."
    )


def format_b_recovered(*, last_cycle_at: str, silent_minutes: float) -> str:
    return (
        "*KILL-SWITCH WATCHDOG: Process B RECOVERED*\n"
        f"Heartbeat fresh again at {last_cycle_at} "
        f"(was silent {silent_minutes:.1f} min). "
        "Kill-switch is operational; A-side orders may resume."
    )
