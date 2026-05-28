"""Tests for tools.observability.run_and_push — Fix 2 durable patch coverage.

The wrapper spawns claude.exe via ``subprocess.run``. On Windows, that
spawn must include ``creationflags=subprocess.CREATE_NO_WINDOW`` (= 0x08000000)
to suppress the console window flicker. On non-Windows platforms,
``subprocess.CREATE_NO_WINDOW`` doesn't exist and the wrapper falls back
to ``creationflags=0`` (no-op).

These tests pin both behaviors so a future refactor can't silently drop
the flag.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from tools.observability import run_and_push


def test_create_no_window_constant_resolves():
    """The module-level _CREATE_NO_WINDOW is a non-negative int."""
    assert isinstance(run_and_push._CREATE_NO_WINDOW, int)
    assert run_and_push._CREATE_NO_WINDOW >= 0


def test_create_no_window_matches_subprocess_attr_on_windows():
    """On Windows, _CREATE_NO_WINDOW must equal subprocess.CREATE_NO_WINDOW."""
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        assert run_and_push._CREATE_NO_WINDOW == subprocess.CREATE_NO_WINDOW
    else:
        assert run_and_push._CREATE_NO_WINDOW == 0


def test_run_and_push_passes_creationflags_to_subprocess(monkeypatch, tmp_path):
    """The subprocess.run call must include creationflags=_CREATE_NO_WINDOW."""
    captured = {}

    def _fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="hello\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    # Discord push is no-op when webhook isn't set; force the skip path.
    monkeypatch.setattr(
        "tools.observability.run_and_push.post_to_channel",
        lambda *a, **kw: "skipped",
    )

    exit_code = run_and_push.run_and_push(
        claude_exe="C:/fake/claude.exe",
        slash_command="/auto-paper",
        discord_channel="paper-auto-entry",
        project_root=str(tmp_path),
    )

    assert exit_code == 0
    assert "creationflags" in captured["kwargs"], (
        "subprocess.run must be invoked with creationflags=_CREATE_NO_WINDOW; "
        "without it the claude.exe spawn flashes a console window on Windows."
    )
    assert captured["kwargs"]["creationflags"] == run_and_push._CREATE_NO_WINDOW


def test_run_and_push_returns_subprocess_exit_code(monkeypatch, tmp_path):
    """Sanity: the wrapper returns the slash command's exit code unchanged."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=42, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        "tools.observability.run_and_push.post_to_channel",
        lambda *a, **kw: "skipped",
    )
    exit_code = run_and_push.run_and_push(
        claude_exe="C:/fake/claude.exe",
        slash_command="/auto-paper",
        discord_channel="paper-auto-entry",
        project_root=str(tmp_path),
    )
    assert exit_code == 42
