"""Tests for tools.backtest.trailing_stop + simulator trail integration."""
from __future__ import annotations

import pandas as pd
import pytest

from tools.backtest.setup_replay import TradeSignal
from tools.backtest.simulator import simulate_trade
from tools.backtest.trailing_stop import (
    TrailConfig,
    make_policy,
    trail_exit_signal,
)


def _df(closes, *, opens=None, highs=None, lows=None, start="2024-01-02"):
    n = len(closes)
    opens = opens if opens is not None else closes
    highs = highs if highs is not None else [c + 0.5 for c in closes]
    lows = lows if lows is not None else [c - 0.5 for c in closes]
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes,
         "Volume": [1_000_000] * n},
        index=pd.date_range(start, periods=n, freq="B"),
    )


def _make_signal(df, fill_idx, entry=100.0, stop=95.0, target=200.0, max_hold=20):
    idx_dates = [pd.Timestamp(ts).date() for ts in df.index]
    return TradeSignal(
        ticker="TEST",
        setup_type="SEPA-VCP",
        setup_grade="A",
        entry_date=idx_dates[max(0, fill_idx - 1)],
        fill_date=idx_dates[fill_idx],
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        max_hold_days=max_hold,
        atr_at_signal=2.0,
    )


# ---------- TrailConfig --------------------------------------------------


def test_trailconfig_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unknown trail mode"):
        TrailConfig(mode="bogus")


def test_trailconfig_rejects_nonpositive_period():
    with pytest.raises(ValueError, match="ma_period"):
        TrailConfig(mode="ma_trail", ma_period=0)


# ---------- ratchet ------------------------------------------------------


def test_ratchet_holds_stop_below_5pct_gain():
    """Closes go from 100 to 103 (+3%) — no trail (threshold is +5%)."""
    fn = make_policy(TrailConfig(mode="ratchet"))
    df = _df([101, 102, 103])
    new = fn(current_stop=95.0, entry_price=100.0, ohlcv_so_far=df)
    assert new == 95.0


def test_ratchet_moves_to_breakeven_at_5pct():
    """Closes reach 105 (+5%) — trail to breakeven (=entry)."""
    fn = make_policy(TrailConfig(mode="ratchet"))
    df = _df([101, 105])
    new = fn(current_stop=95.0, entry_price=100.0, ohlcv_so_far=df)
    assert new == 100.0


def test_ratchet_moves_to_plus5_at_10pct():
    """Closes reach 110 (+10%) — trail to entry+5% = 105."""
    fn = make_policy(TrailConfig(mode="ratchet"))
    df = _df([105, 108, 110])
    new = fn(current_stop=100.0, entry_price=100.0, ohlcv_so_far=df)
    assert new == 105.0


def test_ratchet_never_widens():
    """Existing stop above proposed → stop stays."""
    fn = make_policy(TrailConfig(mode="ratchet"))
    df = _df([105])
    new = fn(current_stop=102.0, entry_price=100.0, ohlcv_so_far=df)
    assert new == 102.0


def test_ratchet_uses_best_close_not_current():
    """Best-ever close was 110, current is 102. Trail stays at +5% (105)."""
    fn = make_policy(TrailConfig(mode="ratchet"))
    df = _df([102, 110, 102])
    new = fn(current_stop=100.0, entry_price=100.0, ohlcv_so_far=df)
    assert new == 105.0  # +10% achieved earlier → ratcheted to +5%


# ---------- ma_trail -----------------------------------------------------


def test_ma_trail_does_nothing_before_period():
    fn = make_policy(TrailConfig(mode="ma_trail", ma_period=10))
    df = _df([105, 106, 107])  # only 3 bars; need 10
    new = fn(current_stop=95.0, entry_price=100.0, ohlcv_so_far=df)
    assert new == 95.0


def test_ma_trail_moves_to_sma_once_period_reached():
    fn = make_policy(TrailConfig(mode="ma_trail", ma_period=3))
    df = _df([100, 105, 110])  # SMA(3) = 105
    new = fn(current_stop=95.0, entry_price=100.0, ohlcv_so_far=df)
    assert new == 105.0


def test_ma_trail_never_widens_on_sma_dip():
    fn = make_policy(TrailConfig(mode="ma_trail", ma_period=3))
    df = _df([110, 105, 100])  # SMA dropping
    new = fn(current_stop=108.0, entry_price=100.0, ohlcv_so_far=df)
    assert new == 108.0  # SMA = 105 but stop stays at 108


# ---------- trail_exit_signal -------------------------------------------


def test_exit_signal_fixed_uses_intrabar_low():
    cfg = TrailConfig(mode="fixed")
    assert trail_exit_signal(cfg, bar_close=110, bar_low=94, current_stop=95) is True
    assert trail_exit_signal(cfg, bar_close=110, bar_low=96, current_stop=95) is False


def test_exit_signal_ma_trail_uses_close():
    cfg = TrailConfig(mode="ma_trail", ma_period=10)
    # Close above trail → hold even if intrabar low dipped below.
    assert trail_exit_signal(cfg, bar_close=105, bar_low=99, current_stop=100) is False
    # Close below trail → exit (Kullamägi rule).
    assert trail_exit_signal(cfg, bar_close=99, bar_low=98, current_stop=100) is True


# ---------- simulator integration ---------------------------------------


def test_simulator_default_is_fixed():
    """Old behavior preserved when trail_config is None."""
    df = _df([100, 99, 96, 100], lows=[99, 97, 94, 99])
    sig = _make_signal(df, fill_idx=0, entry=100, stop=95, target=110)
    out = simulate_trade(sig, df)  # no trail_config → fixed
    assert out.exit_reason == "stop_hit"
    assert out.exit_price == 95.0


def test_simulator_ratchet_locks_in_profit():
    """Closes go 100 → 110 (ratchet to +5% = 105) → 102 (hits trailed stop)."""
    closes = [100, 105, 110, 108, 102]
    lows = [99, 104, 109, 100, 99]      # bar 4 low dips to 99 — below the ratcheted 105
    df = _df(closes, lows=lows)
    sig = _make_signal(df, fill_idx=0, entry=100, stop=95, target=200)
    out = simulate_trade(sig, df, trail_config=TrailConfig(mode="ratchet"))
    assert out.exit_reason == "trail_stop_hit"
    assert out.exit_price == 105.0  # ratcheted from 95 → 100 → 105
    assert out.final_stop == 105.0


def test_simulator_ma_trail_exits_on_close_below_sma():
    """MA-3 trail; price climbs then closes below the SMA → exit at close.

    Bars closes [100, 105, 110, 95]; SMA(3) after bar 2 = 105. Bar 3 opens
    at 107 (above trail to avoid gap-through), low 95, close 95 → close
    below trail-stop=105 → exit at close.
    """
    closes = [100, 105, 110, 95]
    opens = [100, 105, 110, 107]  # bar 3 gaps DOWN intraday but opens above trail
    lows = [99, 104, 108, 90]
    df = _df(closes, opens=opens, lows=lows)
    sig = _make_signal(df, fill_idx=0, entry=100, stop=90, target=200, max_hold=5)
    out = simulate_trade(sig, df, trail_config=TrailConfig(mode="ma_trail", ma_period=3))
    assert out.exit_reason == "trail_stop_hit"
    assert out.exit_price == 95.0  # filled at close, not at SMA
    assert out.final_stop == 105.0


def test_simulator_target_still_wins_over_trail():
    """If target hits BEFORE the trail tightens past the bar, target fills."""
    df = _df([100, 105, 115], highs=[101, 106, 116])
    sig = _make_signal(df, fill_idx=0, entry=100, stop=95, target=110)
    out = simulate_trade(sig, df, trail_config=TrailConfig(mode="ratchet"))
    assert out.exit_reason == "target_hit"
    assert out.exit_price == 110.0
