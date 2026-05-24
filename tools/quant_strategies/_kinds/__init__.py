"""Strategy kind plugins.

Each module exposes ``precompute()`` (optional cross-sectional pass) and
``replay()`` (per-ticker signal generation). The :mod:`tools.quant_strategies.runner`
resolves a YAML spec's ``kind`` field to a module here via :data:`KIND_REGISTRY`.
"""
from __future__ import annotations

from . import clenow_momentum

KIND_REGISTRY: dict[str, object] = {
    clenow_momentum.KIND: clenow_momentum,
}
