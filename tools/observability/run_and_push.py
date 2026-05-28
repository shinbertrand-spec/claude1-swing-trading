"""Wrapper that runs a Claude --print slash command, captures stdout, and
pushes it to a Discord channel via webhook.

Used by the auto-paper cron tasks (and any other observability-instrumented
cron) so the cron's output isn't lost to Task Scheduler's hidden window.

The slash command always runs. The Discord push is BEST-EFFORT — if the
webhook isn't configured or the network fails, the wrapper still exits
with the slash command's exit code so cron health reflects the actual
work, not the observability layer.

CLI usage:

    uv run python -m tools.observability.run_and_push \\
        --claude-exe "C:\\path\\to\\claude.exe" \\
        --slash-command /auto-paper \\
        --discord-channel paper-auto-entry \\
        [--project-root .]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from tools.observability.discord import post_to_channel


# Windows: spawn the slash-command subprocess WITHOUT a visible console
# window. The Task Scheduler action already passes -WindowStyle Hidden to
# powershell.exe, but the powershell shell still creates a brief console
# flash when it spawns claude.exe — only the CREATE_NO_WINDOW process
# creation flag fully suppresses it. The flag is Windows-only; on
# Linux / macOS subprocess has no CREATE_NO_WINDOW attribute so the
# getattr fallback yields 0 (no-op). Part of the 2026-05-28 Fix 2
# durable patch alongside the S4U bake-in across install-*.ps1 scripts.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_and_push(
    *,
    claude_exe: str,
    slash_command: str,
    discord_channel: str,
    project_root: Optional[str] = None,
    timeout_seconds: float = 1200.0,
) -> int:
    """Run the slash command, capture stdout+stderr, push to Discord.

    Returns the slash command's exit code. Discord push failures don't
    affect the return value.
    """
    cwd = project_root or os.getcwd()
    started_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")

    try:
        proc = subprocess.run(
            [
                claude_exe,
                "--print",
                "--permission-mode",
                "bypassPermissions",
                slash_command,
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            creationflags=_CREATE_NO_WINDOW,
        )
        exit_code = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired:
        exit_code = 124  # convention for timeout
        stdout = ""
        stderr = f"timeout after {timeout_seconds}s"
    except FileNotFoundError as exc:
        exit_code = 127
        stdout = ""
        stderr = f"claude exe not found: {exc}"

    # Compose Discord message: short header + slash command output (truncated downstream).
    header = f"**{slash_command}** (exit {exit_code}) — {started_at}"
    body = stdout.strip() or "_(empty stdout)_"
    if stderr.strip():
        body += f"\n\n_stderr:_\n```\n{stderr.strip()[:500]}\n```"
    message = f"{header}\n{body}"

    try:
        result = post_to_channel(
            discord_channel,
            message,
            skip_if_no_webhook=True,
        )
        # Surface push result on the cron's own stdout for log inspection.
        print(f"[discord] {result}")
    except Exception as exc:  # pragma: no cover - defensive: observability must never crash
        print(f"[discord] push exception (non-fatal): {exc!r}")

    return exit_code


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="tools.observability.run_and_push")
    p.add_argument("--claude-exe", required=True)
    p.add_argument("--slash-command", required=True)
    p.add_argument("--discord-channel", required=True)
    p.add_argument("--project-root", default=None)
    p.add_argument("--timeout-seconds", type=float, default=1200.0)
    args = p.parse_args(argv)

    return run_and_push(
        claude_exe=args.claude_exe,
        slash_command=args.slash_command,
        discord_channel=args.discord_channel,
        project_root=args.project_root,
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
