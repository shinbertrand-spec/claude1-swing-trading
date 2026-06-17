"""Security master — point-in-time liquidity (dollar-ADV) + market cap.

Feeds the net-of-cost gate (Phase 1, 2026-06-17):
  * dollar-ADV drives the effective-spread tier AND the sqrt-law impact term
    (tools.backtest.cost_model).
  * market cap drives the universe screen (cap > $500M) at build time.

MODELING NOTE (spread tier keyed on dollar-ADV, not a cap snapshot):
The spec asked for "effective spread by market-cap bucket." Effective spread is
driven by *liquidity*, and cap ≈ dollar-ADV are collinear for the screen we use
(cap > $500M, ADV > $3-5M). We key the spread tier off **dollar-ADV** because it
is computable POINT-IN-TIME from the OHLCV cache, whereas market cap over a
multi-year backtest would use today's shares-outstanding × past price — a
look-ahead/staleness error. The tier bp ranges still match the spec's cap
buckets (mid ~15-40bps, small ~40-150bps). Cap is retained for the universe
screen, where a single recent snapshot is acceptable (documented caveat).

All spreads are QUOTED RETAIL effective spreads (round-trip / 2 = half-spread
paid per side), NOT institutional cost estimates — per the spec.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd

from . import data_cache

# Trailing window (trading days) for the ADV estimate. 60 ≈ one quarter — long
# enough to smooth earnings-week volume spikes, short enough to track regime.
ADV_WINDOW_DEFAULT = 60


@dataclass(frozen=True)
class LiquidityTier:
    """A dollar-ADV liquidity tier and its retail half-spread (bps per side)."""
    name: str
    min_dollar_adv: float      # inclusive lower bound, USD/day
    half_spread_bps: float     # one-side effective half-spread, basis points


# Tiers ordered high→low liquidity. half_spread_bps is HALF the quoted effective
# spread (cost paid on ONE side); a round-trip pays ~2×. Ranges chosen so the
# mid/small *full* spreads land in the spec's bands (mid ~15-40bps full → ~8-20
# half; small ~40-150bps full → ~20-75 half). Mega/large names trade ~1-5bps.
LIQUIDITY_TIERS: tuple[LiquidityTier, ...] = (
    LiquidityTier("mega",  100_000_000.0, 1.5),    # >$100M/day  (full ~3bps)
    LiquidityTier("large",  20_000_000.0, 5.0),    # $20-100M/day (full ~10bps)
    LiquidityTier("mid",     5_000_000.0, 12.0),   # $5-20M/day   (full ~24bps)
    LiquidityTier("small",   1_000_000.0, 35.0),   # $1-5M/day    (full ~70bps)
    LiquidityTier("micro",           0.0, 90.0),   # <$1M/day     (full ~180bps)
)


def dollar_adv(
    df: pd.DataFrame,
    asof: date,
    *,
    window: int = ADV_WINDOW_DEFAULT,
) -> Optional[float]:
    """Median dollar volume (Close × Volume) over the ``window`` bars at/before
    ``asof``. Point-in-time: only uses rows with index date <= asof.

    Returns None if fewer than ``window`` bars are available (too thin to trust).
    Median (not mean) so a single block-trade day doesn't inflate the estimate.
    """
    if df is None or df.empty or "Close" not in df.columns or "Volume" not in df.columns:
        return None
    idx = pd.to_datetime(df.index).date
    mask = idx <= asof
    hist = df.loc[mask]
    if len(hist) < window:
        return None
    tail = hist.iloc[-window:]
    dollar_vol = (tail["Close"].astype(float) * tail["Volume"].astype(float))
    val = float(dollar_vol.median())
    return val if val > 0 else None


def liquidity_tier(dollar_adv_value: Optional[float]) -> LiquidityTier:
    """Map a dollar-ADV value to its liquidity tier. None → most-conservative
    (micro) tier, so unknown liquidity is penalised, never rewarded."""
    if dollar_adv_value is None:
        return LIQUIDITY_TIERS[-1]
    for tier in LIQUIDITY_TIERS:
        if dollar_adv_value >= tier.min_dollar_adv:
            return tier
    return LIQUIDITY_TIERS[-1]


def half_spread_bps(df: pd.DataFrame, asof: date, *, window: int = ADV_WINDOW_DEFAULT) -> float:
    """Convenience: point-in-time retail half-spread (bps) for a ticker bar set."""
    return liquidity_tier(dollar_adv(df, asof, window=window)).half_spread_bps


def dollar_adv_from_cache(
    ticker: str,
    asof: date,
    *,
    window: int = ADV_WINDOW_DEFAULT,
) -> Optional[float]:
    """dollar_adv() sourced from the on-disk OHLCV cache. Returns None if the
    ticker isn't cached (caller decides whether that's fatal)."""
    try:
        df = data_cache.load(ticker)
    except Exception:
        return None
    return dollar_adv(df, asof, window=window)
