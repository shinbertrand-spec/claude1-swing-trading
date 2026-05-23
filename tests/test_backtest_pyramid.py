"""Tests for tools.backtest.pyramid_simulator."""
from __future__ import annotations

import pandas as pd
import pytest

from tools.backtest.pyramid_simulator import (
    PyramidPolicy,
    simulate_trade_pyramided,
    STARTER_SHARES_FRACTION,
    ADDON_1_SHARES_FRACTION,
    ADDON_2_SHARES_FRACTION,
)
from tools.backtest.setup_replay import TradeSignal


def _df(closes, *, opens=None, highs=None, lows=None, volumes=None,
        start="2024-01-02"):
    n = len(closes)
    opens = opens if opens is not None else closes
    highs = highs if highs is not None else [c + 0.5 for c in closes]
    lows = lows if lows is not None else [c - 0.5 for c in closes]
    volumes = volumes if volumes is not None else [1_000_000] * n
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes,
         "Volume": volumes},
        index=pd.date_range(start, periods=n, freq="B"),
    )


def _make_signal(
    df, fill_idx, *, entry=100.0, stop=95.0, target=200.0,
    max_hold=30, grade="A", setup="SEPA-VCP",
):
    idx_dates = [pd.Timestamp(ts).date() for ts in df.index]
    if fill_idx < 1:
        raise ValueError("fill_idx must be >= 1 for pyramid (need anchor bar)")
    return TradeSignal(
        ticker="TEST",
        setup_type=setup,
        setup_grade=grade,
        entry_date=idx_dates[fill_idx - 1],
        fill_date=idx_dates[fill_idx],
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        max_hold_days=max_hold,
        atr_at_signal=2.0,
        notes={"starter_trigger": "VCPBreakout"},
    )


# ---------- Single-leg / back-compat ----------------------------------


def test_disabled_policy_behaves_like_single_leg():
    """With pyramid disabled, only STARTER fills and exit math matches
    the per-leg case."""
    df = _df([100] * 5 + [99, 98, 97, 95, 94])  # tilt down to hit stop
    sig = _make_signal(df, fill_idx=1, entry=100, stop=95, target=120)
    out = simulate_trade_pyramided(
        sig, df, pyramid_policy=PyramidPolicy(enabled=False)
    )
    assert len(out.legs) == 1
    assert out.legs[0].name == "STARTER"
    assert out.addon_1_filled is False
    assert out.addon_2_filled is False
    assert out.exit_reason == "stop_hit"


# ---------- ADD-ON #1: Momentum Burst ---------------------------------


def _calm_then_burst(n_calm: int, post_fill_closes: list[float],
                     post_fill_volumes: list[int] | None = None) -> pd.DataFrame:
    """Helper: ``n_calm`` bars at close=100 (INCLUDING the fill bar at
    fill_idx=n_calm-1) followed by ``post_fill_closes``.

    Fill-bar prev_close == fill-bar close == 100; first post-fill bar's
    day_pct = post_fill_closes[0]/100 - 1.
    """
    closes = [100.0] * n_calm + post_fill_closes
    if post_fill_volumes is None:
        post_fill_volumes = [1_000_000] * len(post_fill_closes)
    volumes = [1_000_000] * n_calm + post_fill_volumes
    return _df(closes, volumes=volumes)


def test_addon_1_fires_on_momentum_burst_and_migrates_stop_to_breakeven():
    """Day +1 post-fill: close 110 (+10% on 1.6× vol) → Momentum Burst fires,
    chase-check passes (110 > 105), ADD-ON #1 fills at 110."""
    df = _calm_then_burst(
        n_calm=32,                          # bars 0..31 at 100 (fill at 31)
        post_fill_closes=[110, 111, 112, 113, 114, 115],
        post_fill_volumes=[1_600_000] + [1_000_000] * 5,
    )
    sig = _make_signal(df, fill_idx=31, entry=100, stop=95, target=200, max_hold=30)
    out = simulate_trade_pyramided(sig, df, pyramid_policy=PyramidPolicy(enabled=True))
    assert out.addon_1_filled is True
    addon_1 = next(leg for leg in out.legs if leg.name == "ADD-ON #1")
    assert addon_1.shares_fraction == ADDON_1_SHARES_FRACTION
    assert addon_1.fill_price == 110.0
    expected_be = (
        100 * STARTER_SHARES_FRACTION
        + addon_1.fill_price * ADDON_1_SHARES_FRACTION
    )
    assert out.combined_breakeven == pytest.approx(expected_be)
    # Stop migrated to combined breakeven.
    assert out.final_stop >= expected_be - 1e-6


def test_addon_1_chase_check_skips_if_close_below_starter_plus_5pct():
    """Momentum Burst fires but close ≤ starter*1.05 → chase-check skip."""
    df = _calm_then_burst(
        n_calm=33,                                         # extra calm day before burst
        post_fill_closes=[100, 104],                        # bar 33: +4% on 1.6× vol
        post_fill_volumes=[1_000_000, 1_600_000],
    )
    sig = _make_signal(df, fill_idx=31, entry=100, stop=95, target=200, max_hold=30)
    out = simulate_trade_pyramided(sig, df, pyramid_policy=PyramidPolicy(enabled=True))
    # 104 ≤ 100*1.05 = 105 → chase-check fires
    assert out.addon_1_filled is False
    assert any("chase_check" in r for r in out.skipped_addon_reasons)


def test_addon_1_skipped_if_burst_fires_after_window():
    """Momentum Burst on bar 11 post-fill (> default 10-bar window) → no addon."""
    df = _calm_then_burst(
        n_calm=32,
        post_fill_closes=[100] * 10 + [110, 111, 112],
        post_fill_volumes=[1_000_000] * 10 + [1_600_000, 1_000_000, 1_000_000],
    )
    sig = _make_signal(df, fill_idx=31, entry=100, stop=95, target=200, max_hold=30)
    out = simulate_trade_pyramided(
        sig, df,
        pyramid_policy=PyramidPolicy(enabled=True, addon_1_max_window_bars=10),
    )
    assert out.addon_1_filled is False


# ---------- ADD-ON #2: Day 7 milestone + grade gate ------------------


def test_addon_2_skipped_for_swan_grade():
    """Grade Swan (not Super Swan/Golden EP) → addon_2 never fires even
    if addon_1 filled and Day 7 milestone passes."""
    df = _calm_then_burst(
        n_calm=32,
        post_fill_closes=[110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120],
        post_fill_volumes=[1_600_000] + [1_000_000] * 10,
    )
    sig = _make_signal(df, fill_idx=31, entry=100, stop=95, target=200,
                       max_hold=30, grade="Swan")
    out = simulate_trade_pyramided(
        sig, df,
        pyramid_policy=PyramidPolicy(enabled=True, regime_class="stage_2_confirmed"),
    )
    assert out.addon_1_filled is True   # addon_1 fires (no grade gate)
    assert out.addon_2_filled is False
    assert any("addon_2_skipped_grade" in r for r in out.skipped_addon_reasons)


def test_addon_2_skipped_for_wrong_regime():
    """Even SuperSwan grade fails ADD-ON #2 in a non-stage_2_confirmed regime."""
    df = _calm_then_burst(
        n_calm=32,
        post_fill_closes=[110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120],
        post_fill_volumes=[1_600_000] + [1_000_000] * 10,
    )
    sig = _make_signal(df, fill_idx=31, entry=100, stop=95, target=200,
                       max_hold=30, grade="SuperSwan")
    out = simulate_trade_pyramided(
        sig, df,
        pyramid_policy=PyramidPolicy(enabled=True, regime_class="stage_2_weakening"),
    )
    assert out.addon_1_filled is True   # addon_1 fires (no regime gate on it)
    assert out.addon_2_filled is False
    assert any("addon_2_skipped_regime" in r for r in out.skipped_addon_reasons)


def test_addon_2_fires_for_super_swan_in_stage_2():
    """SuperSwan + stage_2_confirmed regime + ADD-ON #1 filled + survived
    Day 7 (no break of anchor low, no close below MA) → ADD-ON #2 fires."""
    df = _calm_then_burst(
        n_calm=32,
        post_fill_closes=[110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120],
        post_fill_volumes=[1_600_000] + [1_000_000] * 10,
    )
    sig = _make_signal(df, fill_idx=31, entry=100, stop=95, target=200,
                       max_hold=30, grade="SuperSwan")
    out = simulate_trade_pyramided(
        sig, df,
        pyramid_policy=PyramidPolicy(enabled=True, regime_class="stage_2_confirmed"),
    )
    assert out.addon_1_filled is True
    assert out.addon_2_filled is True
    addon_2 = next(leg for leg in out.legs if leg.name == "ADD-ON #2")
    assert addon_2.shares_fraction == ADDON_2_SHARES_FRACTION
    assert addon_2.trigger == "Day7Milestone"


# ---------- combined R-multiple --------------------------------------


def test_combined_r_multiple_weighted_correctly():
    """STARTER 100 + ADD-ON #1 at 110 → BE = (1/3·100 + 2/3·110) = 106.67.
    Target 125 hit → per-leg: starter gain 25, addon gain 15. Total
    weighted per-share = (1/3·25 + 2/3·15) = 18.33; / starter-risk 5 = 3.67 R."""
    df = _calm_then_burst(
        n_calm=32,
        post_fill_closes=[110, 115, 120, 121, 122, 125],
        post_fill_volumes=[1_600_000] + [1_000_000] * 5,
    )
    sig = _make_signal(df, fill_idx=31, entry=100, stop=95, target=125, max_hold=15)
    out = simulate_trade_pyramided(sig, df, pyramid_policy=PyramidPolicy(enabled=True))
    assert out.addon_1_filled is True
    assert out.exit_reason == "target_hit"
    assert out.exit_price == 125.0
    starter_gain = 125 - 100   # 25
    addon_1_leg = next(leg for leg in out.legs if leg.name == "ADD-ON #1")
    addon_1_gain = 125 - addon_1_leg.fill_price
    total_per_share = (
        STARTER_SHARES_FRACTION * starter_gain
        + ADDON_1_SHARES_FRACTION * addon_1_gain
    )
    expected_r = total_per_share / 5.0  # starter_risk = entry-stop = 5
    assert out.r_multiple == pytest.approx(expected_r)
