"""Registered-universe loader for quant strategy specs.

Single source of truth for ticker universes. Strategy YAMLs can either
embed a literal ``universe.tickers:`` list (legacy/ad-hoc) or reference
a registered universe by ``universe.name:``. The latter resolves to
the YAML file at ``tools/quant_strategies/_universes/<name>.yml``.

The loader is intentionally read-only and side-effect-free: it parses
the registered file and returns the ticker list. Universe files are
versioned-by-content: once published, do not mutate. Publish a new
file (e.g. ``sp500_2026q3.yml``) when the membership changes.

Both consumers — ``tools.quant_strategies.runner`` and
``tools.auto_paper.quant_scanner`` — should resolve a spec's universe
via :func:`resolve_universe_tickers` rather than reading
``spec["universe"]["tickers"]`` directly, so the registered-name form
is handled transparently.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


_UNIVERSES_DIR = Path(__file__).parent / "_universes"


class UniverseError(ValueError):
    """Raised when a registered-universe reference cannot be resolved."""


def universes_dir() -> Path:
    """Return the directory holding registered-universe YAMLs."""
    return _UNIVERSES_DIR


@lru_cache(maxsize=None)
def _load_universe_file(name: str) -> dict[str, Any]:
    """Load and validate a registered-universe YAML file.

    Cached: registered universes are immutable-by-convention, so a single
    parse per process is correct. The :func:`reset_cache` hook exists for
    test isolation.
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise UniverseError(f"invalid universe name {name!r}")
    path = _UNIVERSES_DIR / f"{name}.yml"
    if not path.is_file():
        available = sorted(p.stem for p in _UNIVERSES_DIR.glob("*.yml"))
        raise UniverseError(
            f"no registered universe {name!r} at {path}; "
            f"available: {available}"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise UniverseError(f"universe file {path} must be a YAML mapping")
    if data.get("name") != name:
        raise UniverseError(
            f"universe file {path} has name={data.get('name')!r}; "
            f"expected {name!r}"
        )
    tickers = data.get("tickers")
    if not isinstance(tickers, list) or not tickers:
        raise UniverseError(f"universe file {path} has no non-empty tickers list")
    if not all(isinstance(t, str) and t.strip() for t in tickers):
        raise UniverseError(f"universe file {path} has non-string ticker entries")
    return data


def get_universe(name: str) -> list[str]:
    """Return the ticker list for a registered universe (a fresh copy)."""
    return list(_load_universe_file(name)["tickers"])


def get_universe_metadata(name: str) -> dict[str, Any]:
    """Return the metadata block for a registered universe.

    Includes ``pinned_at`` / ``provenance`` / ``notes`` etc. — everything
    except the ticker list itself.
    """
    data = dict(_load_universe_file(name))
    data.pop("tickers", None)
    return data


def list_universes() -> list[str]:
    """List registered universe names available on disk."""
    return sorted(p.stem for p in _UNIVERSES_DIR.glob("*.yml"))


def resolve_universe_tickers(spec: dict[str, Any]) -> list[str]:
    """Resolve a strategy spec's universe to a concrete ticker list.

    Accepts either form on ``spec["universe"]``:

    * ``{"name": "<registered>", "benchmark": "SPY"}`` — looked up via
      :func:`get_universe`.
    * ``{"tickers": [...], "benchmark": "SPY"}`` — used as-is.

    Both forms must carry ``benchmark`` (caller handles that field
    separately). If both ``name`` and ``tickers`` are present, ``name``
    wins and ``tickers`` is ignored — this lets a refactor land a
    ``name:`` line without forcing simultaneous removal of an inline
    list during transition.

    Returns a fresh list; callers may mutate it (e.g. to append a
    benchmark) without affecting cached state.
    """
    if "universe" not in spec or not isinstance(spec["universe"], dict):
        raise UniverseError("spec missing 'universe' mapping")
    u = spec["universe"]
    if "name" in u:
        return get_universe(u["name"])
    if "tickers" in u:
        tickers = u["tickers"]
        if not isinstance(tickers, list) or not tickers:
            raise UniverseError("spec universe.tickers must be a non-empty list")
        return list(tickers)
    raise UniverseError("spec universe missing both 'name' and 'tickers'")


def reset_cache() -> None:
    """Drop the cached parses. For test isolation only."""
    _load_universe_file.cache_clear()
