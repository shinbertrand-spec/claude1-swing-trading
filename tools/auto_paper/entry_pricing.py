"""Entry-limit pricing, split by setup class.

Why this module exists (the 2026-06-16 no-fill bug)
----------------------------------------------------
The deployed KIND backtests fill at the NEXT BAR'S OPEN, unconditionally
(``tools/quant_strategies/_kinds/*.py`` → ``entry_price = float(next_bar["Open"])``).
So the deployment gate was VALIDATED on next-open fills. But the live path
placed a resting LIMIT BUY at ``pivot * 1.001`` (prior close + 0.1%). A momentum
name that gaps UP at the open never trades back down to that limit → no fill all
session (the 2026-06-10 .. 06-15 outage). Aligning the live fill to the open is
implementing the execution the gate already certified.

Split by setup class
--------------------
Momentum return is realized in the OVERNIGHT GAP (Lou–Polk–Skouras 2019: price
momentum +0.98%/mo overnight vs −0.02% intraday) → momentum MUST fill at the
open, paying up through the gap via a marketable limit. Mean-reversion buys
oversold names, so chasing up is adverse-selection → it rests at/below the prior
close.

We never send a market order (CLAUDE.md forbids it). A DAY *marketable limit*
(limit set above the expected open) fills at the open print whenever
``open <= limit``; the ``MOMENTUM_ENTRY_BUFFER`` caps how far we'll chase so a
pathological gap (> 3%) is skipped rather than filled at any price.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Base KINDs (spec["kind"] values, NOT spec filenames). Universe variants
# (e.g. ts_momentum_liquid_us) resolve to these via resolve_kind().
MOMENTUM_KINDS = {
    "ts_momentum",
    "residual_momentum",
    "clenow_momentum",
    "dual_ma_trend_following",
    # Event-driven insider buying enters at the next-bar open (the event drift
    # is realized over months, not intraday) — marketable, momentum-class fill.
    "event_insider_buying",
}
REVERSION_KINDS = {
    "xs_short_term_reversal",
    "connors_rsi2",
    "xs_low_volatility",
    # Integrated value+momentum is a monthly low-turnover rebalance, not a
    # breakout — rest the limit at the pivot, never chase up.
    "value_momentum_integrated",
}

# Marketable-limit ceiling for momentum: fills at the open whenever the gap is
# <= 3% (price improvement when the gap is smaller; matches the next-open
# backtest fill), and skips a pathological > 3% chase.
MOMENTUM_ENTRY_BUFFER = 0.03

# Spec dir mirrors quant_scanner.SPEC_DIR. Duplicated here to keep this module a
# leaf (no import of quant_scanner → no circular import at placement time).
_SPEC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tools",
    "quant_strategies",
)

# Universe suffixes stripped as a fallback when the spec file can't be read.
_UNIVERSE_SUFFIXES = ("_liquid_us", "_ai_pure", "_ai_broad")


def resolve_kind(setup_type: str) -> str:
    """Resolve a deployable ``setup_type`` (spec filename) to its base KIND.

    Authoritative: read ``spec["kind"]`` from ``tools/quant_strategies/<setup>.yml``.
    Fallback: strip a known universe suffix (e.g. ``_liquid_us``). Last resort:
    return ``setup_type`` unchanged (→ no-chase branch + a warning in
    entry_limit_price).
    """
    spec_path = Path(_SPEC_DIR) / f"{setup_type}.yml"
    if spec_path.is_file():
        try:
            spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
            kind = (spec or {}).get("kind")
            if kind:
                return str(kind)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("resolve_kind: failed to read %s: %r", spec_path, exc)
    for suf in _UNIVERSE_SUFFIXES:
        if setup_type.endswith(suf):
            return setup_type[: -len(suf)]
    return setup_type


def entry_limit_price(kind: str, pivot: float) -> float:
    """Limit price for a buy entry, split by setup class.

    - MOMENTUM kinds → ``pivot * (1 + MOMENTUM_ENTRY_BUFFER)`` (marketable
      limit; fills at the open through the overnight gap, caps the chase at 3%).
    - REVERSION kinds → ``pivot`` (rest at the prior close; never chase up).
    - Unknown kind → ``pivot`` (conservative no-chase) + a WARNING, because an
      unrouted momentum kind would silently keep missing fills.
    """
    if kind in MOMENTUM_KINDS:
        return round(pivot * (1.0 + MOMENTUM_ENTRY_BUFFER), 2)
    if kind in REVERSION_KINDS:
        return round(pivot, 2)
    logger.warning(
        "entry_limit_price: unknown kind %r — defaulting to no-chase (pivot). "
        "If this is a momentum setup it will keep missing fills; add it to "
        "MOMENTUM_KINDS/REVERSION_KINDS.",
        kind,
    )
    return round(pivot, 2)
