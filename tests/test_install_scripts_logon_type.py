"""Anti-drift meta-tests for scripts/install-*.ps1 — Fix 2 durable patch.

Every install-*.ps1 script registers a Windows Task Scheduler task with a
Principal that has a LogonType. The Fix 2 durable patch (2026-05-28)
standardised on ``LogonType S4U`` across all 6 install scripts so:

- No password storage (vs Password / InteractiveOrPassword)
- No interactive logon dependency (vs Interactive — the prior setting that
  required the user to be logged in for the task to fire)
- No console window flicker (S4U runs in a non-interactive session)
- HTTPS internet access works (Tiger API, Anthropic API, yfinance, Discord
  webhook are all anonymous-TLS — no Kerberos / SMB needed). The S4U
  restricted token's network limitation does NOT block this class of calls.

These meta-tests grep the install scripts to prevent silent drift back to
``Interactive`` on future edits. If a new install-*.ps1 lands, add it to
``INSTALL_SCRIPTS`` below.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

INSTALL_SCRIPTS = [
    "install-auto-paper-tasks.ps1",
    "install-x-ingest-tasks.ps1",
    "install-kill-switch-tasks.ps1",
    "install-news-hourly-task.ps1",
    "install-morning-task.ps1",
    "install-position-checker-task.ps1",
]


@pytest.mark.parametrize("script", INSTALL_SCRIPTS)
def test_install_script_uses_s4u_logon_type(script):
    """Every install-*.ps1 must specify -LogonType S4U on its Principal."""
    p = SCRIPTS_DIR / script
    assert p.is_file(), f"missing install script: {p}"
    text = p.read_text(encoding="utf-8")
    # The S4U logon type is the durable Fix 2 setting.
    assert re.search(r"-LogonType\s+S4U", text), (
        f"{script} must use '-LogonType S4U' (Fix 2 durable patch). "
        f"Falling back to 'Interactive' brings back the user-logon dependency "
        f"and console flicker. Update the install script's Principal block."
    )


@pytest.mark.parametrize("script", INSTALL_SCRIPTS)
def test_install_script_never_uses_interactive_logon(script):
    """Defense in depth: no install-*.ps1 should mention LogonType Interactive."""
    p = SCRIPTS_DIR / script
    text = p.read_text(encoding="utf-8")
    assert "-LogonType Interactive" not in text, (
        f"{script} still contains '-LogonType Interactive'. Replace with "
        f"'-LogonType S4U' per the Fix 2 durable patch."
    )


@pytest.mark.parametrize("script", INSTALL_SCRIPTS)
def test_install_script_action_passes_window_style_hidden(script):
    """PowerShell action arg should request hidden window style.

    Belt-and-suspenders with the CREATE_NO_WINDOW process flag set inside
    tools.observability.run_and_push for child claude.exe spawns. The
    Hidden window style covers the powershell.exe parent; CREATE_NO_WINDOW
    covers the claude.exe child.
    """
    p = SCRIPTS_DIR / script
    text = p.read_text(encoding="utf-8")
    assert re.search(r"-WindowStyle\s+Hidden", text), (
        f"{script} action should pass '-WindowStyle Hidden' to powershell.exe"
    )
