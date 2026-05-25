"""Discord channel POST helper via webhook.

Reads webhook URLs from ``~/.claude/channels/discord/.env`` keyed by channel.
Mirrors the storage convention of ``scripts/send-to-telegram.ps1``.

Webhook URLs are CREDENTIALS — never echo to Telegram, journals, or stdout
that lands in observability sinks. The URL contains an unauthenticated
token in the path; anyone with it can post to the channel.

Library usage:

    from tools.observability.discord import post_to_channel
    post_to_channel("paper-auto-entry", "hello world")

CLI usage:

    uv run python -m tools.observability.discord \\
        --channel paper-auto-entry --message "hello world"
    uv run python -m tools.observability.discord \\
        --channel paper-auto-monitor --message-file path/to/summary.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

DEFAULT_ENV_PATH = Path.home() / ".claude" / "channels" / "discord" / ".env"
MAX_CONTENT_LENGTH = 1900  # Discord's hard limit is 2000; leave room for truncation marker
WEBHOOK_URL_PREFIX = "https://discord.com/api/webhooks/"


def _channel_to_env_key(channel: str) -> str:
    return "DISCORD_WEBHOOK_" + channel.upper().replace("-", "_")


def _read_env_value(env_path: Path, key: str) -> Optional[str]:
    if not env_path.is_file():
        return None
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip().strip("\"'")
    return None


def resolve_webhook_url(
    channel: str,
    *,
    env_path: Optional[Path] = None,
) -> Optional[str]:
    """Look up the webhook URL for ``channel``. ``$env:KEY`` wins over .env file."""
    env_key = _channel_to_env_key(channel)
    url = os.environ.get(env_key)
    if url:
        return url
    return _read_env_value(env_path or DEFAULT_ENV_PATH, env_key)


def _truncate(message: str, max_length: int = MAX_CONTENT_LENGTH) -> str:
    if len(message) <= max_length:
        return message
    overflow = len(message) - max_length
    return message[:max_length] + f"\n... [truncated {overflow} chars]"


def post_to_channel(
    channel: str,
    message: str,
    *,
    env_path: Optional[Path] = None,
    max_length: int = MAX_CONTENT_LENGTH,
    skip_if_no_webhook: bool = True,
    timeout_seconds: float = 10.0,
) -> dict:
    """POST ``message`` to ``channel``'s Discord webhook.

    Returns a dict ``{"status": "sent" | "skipped" | "error", ...}``.

    With ``skip_if_no_webhook=True`` (the default), a missing webhook URL
    or empty message returns ``status="skipped"`` rather than raising. This
    lets the cron wrapper install before the user finishes setting up
    Discord channels — once webhooks are added to the .env, the next cron
    tick starts posting automatically.
    """
    if not message or not message.strip():
        if skip_if_no_webhook:
            return {"status": "skipped", "reason": "empty message"}
        raise ValueError("message is empty")

    url = resolve_webhook_url(channel, env_path=env_path)
    if not url:
        if skip_if_no_webhook:
            return {
                "status": "skipped",
                "reason": f"no webhook configured for channel {channel!r} "
                f"(set {_channel_to_env_key(channel)} in env or {env_path or DEFAULT_ENV_PATH})",
            }
        raise RuntimeError(
            f"no webhook configured for channel {channel!r}; "
            f"set {_channel_to_env_key(channel)} in env or {env_path or DEFAULT_ENV_PATH}"
        )
    if not url.startswith(WEBHOOK_URL_PREFIX):
        return {
            "status": "error",
            "reason": f"webhook URL does not start with {WEBHOOK_URL_PREFIX}",
        }

    body = json.dumps({"content": _truncate(message, max_length)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            return {
                "status": "sent",
                "channel": channel,
                "http_status": resp.status,
                "length": len(message),
            }
    except urllib.error.HTTPError as exc:
        return {
            "status": "error",
            "channel": channel,
            "http_status": exc.code,
            "reason": exc.reason,
        }
    except urllib.error.URLError as exc:
        return {"status": "error", "channel": channel, "reason": str(exc.reason)}
    except OSError as exc:
        return {"status": "error", "channel": channel, "reason": str(exc)}


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="tools.observability.discord")
    p.add_argument("--channel", required=True, help="e.g. paper-auto-entry")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--message", help="Inline message text")
    group.add_argument("--message-file", help="Path to a UTF-8 file containing the message")
    p.add_argument(
        "--env-path",
        default=str(DEFAULT_ENV_PATH),
        help="Override default .env path",
    )
    p.add_argument(
        "--no-skip-if-no-webhook",
        action="store_true",
        help="Exit non-zero if the channel's webhook URL is not configured",
    )
    args = p.parse_args(argv)

    if args.message_file:
        message = Path(args.message_file).read_text(encoding="utf-8")
    else:
        message = args.message

    result = post_to_channel(
        args.channel,
        message,
        env_path=Path(args.env_path),
        skip_if_no_webhook=not args.no_skip_if_no_webhook,
    )
    print(json.dumps(result))
    return 0 if result["status"] in ("sent", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
