"""Net-of-cost execution model for the deployment gate (Phase 1, 2026-06-17).

Two components per side of a trade:
  1. Half effective spread (retail quoted) — from the dollar-ADV liquidity tier
     in tools.backtest.security_master.
  2. Market impact — Almgren/Bouchaud SQUARE-ROOT LAW: impact grows with the
     square root of participation (trade size / ADV). Negligible at retail scale
     in liquid names, material in thin names or large clips.

Cost is charged as a price haircut: a BUY fills WORSE (higher) by the cost bps,
a SELL fills WORSE (lower). This is intentionally pessimistic vs the old
zero-cost gate — the whole point of Phase 1 is to stop certifying books that are
gross-positive but net-negative.

Why this matters (the spec's framing): for a few-%-per-event edge, zero-cost
backtests pass net-negative strategies — the same backtest-vs-live divergence
class as the entry-fill bug. Every dedicated insider ETF was liquidated by ~2020
precisely because the edge doesn't survive realistic retail cost.
"""
from __future__ import annotations

import math
from typing import Optional

# Square-root-law coefficient (bps of price move at 1× ADV participation).
# sqrt-law: impact ≈ COEFF * sqrt(trade_$ / ADV_$). At 1× ADV → ~100bps (≈ one
# day's volume moves price ~1%, consistent with σ_daily ~1% and the Almgren
# constant). Retail clips run <<1% participation, so impact is a few bps and the
# spread dominates — but this term correctly punishes thin names / large sizes.
# Literature default; NOT tuned to any strategy's pass/fail.
IMPACT_COEFF_BPS = 100.0

# Fallback impact (bps/side) when ADV is unknown — treat as illiquid, penalise.
UNKNOWN_ADV_IMPACT_BPS = 100.0


def impact_bps(trade_dollars: float, dollar_adv: Optional[float]) -> float:
    """One-side market impact (bps) via the square-root law."""
    if dollar_adv is None or dollar_adv <= 0:
        return UNKNOWN_ADV_IMPACT_BPS
    if trade_dollars <= 0:
        return 0.0
    participation = trade_dollars / dollar_adv
    return IMPACT_COEFF_BPS * math.sqrt(participation)


def one_side_cost_bps(
    trade_dollars: float,
    dollar_adv: Optional[float],
    half_spread_bps: float,
) -> float:
    """Total one-side cost (bps) = half effective spread + sqrt-law impact."""
    return half_spread_bps + impact_bps(trade_dollars, dollar_adv)


def apply_buy_cost(price: float, cost_bps: float) -> float:
    """A buy fills WORSE (higher) by cost_bps."""
    return price * (1.0 + cost_bps / 10_000.0)


def apply_sell_cost(price: float, cost_bps: float) -> float:
    """A sell fills WORSE (lower) by cost_bps."""
    return price * (1.0 - cost_bps / 10_000.0)
