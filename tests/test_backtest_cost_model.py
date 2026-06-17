"""Tests for the net-of-cost execution model (security_master + cost_model)."""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from tools.backtest import cost_model, security_master as sm


def _ohlcv(n: int, close: float, volume: float, end: str = "2024-06-28") -> pd.DataFrame:
    idx = pd.bdate_range(end=end, periods=n)
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": volume},
        index=idx,
    )


# ----------------------------------------------------------- security_master


def test_dollar_adv_median_point_in_time():
    df = _ohlcv(80, close=100.0, volume=50_000)  # $5M/day
    val = sm.dollar_adv(df, date(2024, 6, 28), window=60)
    assert val == pytest.approx(5_000_000.0)


def test_dollar_adv_none_when_too_few_bars():
    df = _ohlcv(40, close=100.0, volume=50_000)
    assert sm.dollar_adv(df, date(2024, 6, 28), window=60) is None


def test_dollar_adv_respects_asof_cutoff():
    df = _ohlcv(80, close=100.0, volume=50_000, end="2024-06-28")
    # asof before the data starts → no bars → None
    assert sm.dollar_adv(df, date(2000, 1, 1), window=60) is None


def test_liquidity_tier_buckets():
    assert sm.liquidity_tier(200_000_000).name == "mega"
    assert sm.liquidity_tier(50_000_000).name == "large"
    assert sm.liquidity_tier(8_000_000).name == "mid"
    assert sm.liquidity_tier(2_000_000).name == "small"
    assert sm.liquidity_tier(500_000).name == "micro"


def test_unknown_liquidity_is_most_conservative():
    # None ADV must map to the WORST (micro) tier — never rewarded.
    assert sm.liquidity_tier(None).name == "micro"
    assert sm.liquidity_tier(None).half_spread_bps == max(
        t.half_spread_bps for t in sm.LIQUIDITY_TIERS
    )


def test_half_spread_monotonic_in_liquidity():
    spreads = [t.half_spread_bps for t in sm.LIQUIDITY_TIERS]
    assert spreads == sorted(spreads)  # mega cheapest → micro priciest


# ------------------------------------------------------------------ cost_model


def test_impact_sqrt_law_shape():
    # 1× ADV participation → ~IMPACT_COEFF_BPS
    assert cost_model.impact_bps(1_000_000, 1_000_000) == pytest.approx(
        cost_model.IMPACT_COEFF_BPS
    )
    # 1% participation → COEFF * sqrt(0.01) = COEFF * 0.1
    assert cost_model.impact_bps(10_000, 1_000_000) == pytest.approx(
        cost_model.IMPACT_COEFF_BPS * 0.1
    )
    # quarter participation → half impact of full
    assert cost_model.impact_bps(250_000, 1_000_000) == pytest.approx(
        cost_model.IMPACT_COEFF_BPS * 0.5
    )


def test_impact_unknown_adv_penalised():
    assert cost_model.impact_bps(10_000, None) == cost_model.UNKNOWN_ADV_IMPACT_BPS
    assert cost_model.impact_bps(10_000, 0) == cost_model.UNKNOWN_ADV_IMPACT_BPS


def test_one_side_cost_adds_spread_and_impact():
    c = cost_model.one_side_cost_bps(10_000, 1_000_000, half_spread_bps=12.0)
    assert c == pytest.approx(12.0 + cost_model.IMPACT_COEFF_BPS * 0.1)


def test_buy_fills_higher_sell_fills_lower():
    assert cost_model.apply_buy_cost(100.0, 50.0) == pytest.approx(100.5)
    assert cost_model.apply_sell_cost(100.0, 50.0) == pytest.approx(99.5)


def test_round_trip_cost_is_pure_loss():
    # Same price in and out, but cost makes the round trip negative.
    buy = cost_model.apply_buy_cost(100.0, 30.0)
    sell = cost_model.apply_sell_cost(100.0, 30.0)
    assert sell < buy  # you always lose the spread+impact on a flat move


def test_thin_name_costs_more_than_liquid_for_same_clip():
    clip = 50_000
    liquid = cost_model.one_side_cost_bps(clip, 100_000_000, sm.liquidity_tier(100_000_000).half_spread_bps)
    thin = cost_model.one_side_cost_bps(clip, 2_000_000, sm.liquidity_tier(2_000_000).half_spread_bps)
    assert thin > liquid
