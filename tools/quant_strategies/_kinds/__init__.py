"""Strategy kind plugins.

Each module exposes ``precompute()`` (optional cross-sectional pass) and
``replay()`` (per-ticker signal generation). The :mod:`tools.quant_strategies.runner`
resolves a YAML spec's ``kind`` field to a module here via :data:`KIND_REGISTRY`.
"""
from __future__ import annotations

from . import clenow_momentum
from . import connors_rsi2
from . import dual_ma_trend_following
from . import event_insider_buying
from . import residual_momentum
from . import ts_momentum
from . import xs_low_volatility
from . import xs_short_term_reversal

KIND_REGISTRY: dict[str, object] = {
    clenow_momentum.KIND: clenow_momentum,
    connors_rsi2.KIND: connors_rsi2,
    dual_ma_trend_following.KIND: dual_ma_trend_following,
    event_insider_buying.KIND: event_insider_buying,
    residual_momentum.KIND: residual_momentum,
    ts_momentum.KIND: ts_momentum,
    xs_low_volatility.KIND: xs_low_volatility,
    xs_short_term_reversal.KIND: xs_short_term_reversal,
}
