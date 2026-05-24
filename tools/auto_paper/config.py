"""Deployable-setup list — single source of truth at tools/deployable_setups.yml.

Loaders consumed by:

* :mod:`tools.auto_paper.pipeline` — filter candidates before placement
* ``.claude/commands/morning-deep-dive.md § 5p`` — human-flow auto-place gate
  (the prompt references this file by path; the agent reads it at run-time)
"""
from __future__ import annotations

import os
from typing import Any

import yaml

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tools",
    "deployable_setups.yml",
)


class DeployableConfigError(RuntimeError):
    """Raised when the deployable-setups YAML can't be loaded."""


def load(path: str | None = None) -> dict[str, Any]:
    """Load + return the deployable-setups YAML as a dict."""
    p = path or DEFAULT_PATH
    if not os.path.isfile(p):
        raise DeployableConfigError(f"deployable-setups file not found: {p}")
    try:
        with open(p, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise DeployableConfigError(f"YAML parse error in {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise DeployableConfigError(f"top-level must be a mapping; got {type(data).__name__}")
    return data


def deployable_setup_names(path: str | None = None) -> set[str]:
    """Return the set of setup-type names that have cleared the deployment gate.

    Variant (sell-aware / loosened+ma_trail / etc.) is NOT part of the key —
    the morning routine only knows the setup TYPE. If you need variant
    awareness, read :func:`load` directly.
    """
    data = load(path)
    items = data.get("deployable", []) or []
    return {row["setup"] for row in items if isinstance(row, dict) and "setup" in row}


def is_deployable(setup_type: str, path: str | None = None) -> bool:
    """Convenience: True iff the named setup type is on the deployable list."""
    return setup_type in deployable_setup_names(path)
