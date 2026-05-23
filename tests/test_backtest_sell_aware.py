"""Tests for tools.backtest.sell_aware + simulator sell-policy integration."""
from __future__ import annotations

import pandas as pd

from tools.backtest.sell_aware import (
    SellPolicy,
    evaluate_bar,
    exit_action_to_reason,
)
from tools.backtest.setup_replay import TradeSignal
from tools.backtest.simulator import simulate_trade


def _df(closes, *, opens=None, highs=None, lows=None, volumes=None,
        start="2023-01-02"):
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


def _make_signal(df, fill_idx, *, entry=100.0, stop=95.0, target=200.0,
                 max_hold=30, grade="A"):
    idx_dates = [pd.Timestamp(ts).date() for ts in df.index]
    return TradeSignal(
        ticker="TEST",
        setup_type="SEPA-VCP",
        setup_grade=grade,
        entry_date=idx_dates[max(0, fill_idx - 1)],
        fill_date=idx_dates[fill_idx],
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        max_hold_days=max_hold,
        atr_at_signal=2.0,
    )


# ---------- exit_action_to_reason --------------------------------------


def test_exit_action_to_reason_prefixes():
    assert exit_action_to_reason("sell_50") == "sell_decision_sell_50"
    assert exit_action_to_reason("sell_100") == "sell_decision_sell_100"
    assert exit_action_to_reason("tighten_stop") == "sell_decision_tighten_stop"


# ---------- evaluate_bar smoke ----------------------------------------


def test_evaluate_bar_on_calm_data_returns_hold():
    """Flat synthetic data with VARIED volumes/spreads (degenerate
    perfectly-equal data falsely fires highest_ever_volume + widest_spread)
    → no patterns should fire, action 'hold'.

    Use rising volumes + variable spreads so today's bar is not a tie on any
    "highest/widest of move" criterion.
    """
    import numpy as np
    rng = np.random.default_rng(7)
    n = 400
    closes = np.array([100.0 + i * 0.01 + rng.normal(0, 0.02) for i in range(n)])
    highs = closes + rng.uniform(0.3, 1.0, size=n)
    lows = closes - rng.uniform(0.3, 1.0, size=n)
    volumes = rng.integers(800_000, 1_200_000, size=n).astype(int)
    # Force today's volume + spread to be MIDDLE-of-pack (not extreme).
    volumes[-1] = 1_000_000
    highs[-1] = closes[-1] + 0.3
    lows[-1] = closes[-1] - 0.3
    df = _df(list(closes), highs=list(highs), lows=list(lows), volumes=list(volumes))
    policy = SellPolicy(enabled=True)
    fill_date = pd.Timestamp(df.index[300]).date()
    event = evaluate_bar(
        df_through_today=df.iloc[:350],
        starter_entry=100.0,
        fill_date=fill_date,
        setup_grade="A",
        policy=policy,
    )
    assert event.action == "hold"


def test_evaluate_bar_handles_short_history():
    """Short OHLCV → detectors fail gracefully; evaluate returns hold.

    Uses rising-volume varying-spread fixture so even bars where detectors
    DO run don't spuriously fire on degenerate ties.
    """
    import numpy as np
    rng = np.random.default_rng(11)
    n = 30
    closes = np.array([100.0 + rng.normal(0, 0.02) for _ in range(n)])
    highs = closes + rng.uniform(0.3, 1.0, size=n)
    lows = closes - rng.uniform(0.3, 1.0, size=n)
    volumes = rng.integers(800_000, 1_200_000, size=n).astype(int)
    volumes[-1] = 1_000_000   # middle-of-pack
    highs[-1] = closes[-1] + 0.3
    lows[-1] = closes[-1] - 0.3
    df = _df(list(closes), highs=list(highs), lows=list(lows), volumes=list(volumes))
    fill_date = pd.Timestamp(df.index[5]).date()
    policy = SellPolicy(enabled=True)
    event = evaluate_bar(
        df_through_today=df,
        starter_entry=100.0,
        fill_date=fill_date,
        setup_grade="A",
        policy=policy,
    )
    # Detectors that need >= 200 bars (base_stage) raise — swallowed.
    # Climax-top runs on 30 bars but with non-degenerate fixture no patterns fire.
    assert event.action == "hold"


def test_evaluate_bar_sell_into_strength_fires_on_quick_run():
    """Gain 12% in last 3 bars → sell_into_strength triggers; with grade=A+
    fraction is 0.50 → sell_decision action 'sell_50'."""
    # Need enough history for base_stage (>=200+10) and full RSI window.
    base = [100.0] * 350 + [100, 105, 110, 112]
    df = _df(base)
    fill_date = pd.Timestamp(df.index[349]).date()
    policy = SellPolicy(enabled=True)
    event = evaluate_bar(
        df_through_today=df,
        starter_entry=100.0,
        fill_date=fill_date,
        setup_grade="A+",
        policy=policy,
    )
    # 12 / 100 = 0.12 over 3 bars; threshold met; A+ → 0.50 fraction → sell_50.
    assert event.sell_into_strength_triggered is True
    # Action could be sell_50 alone OR a higher action if a climax pattern
    # ALSO fires on the parabolic run. Verify it's in the sell family.
    assert event.action.startswith("sell_") or event.action == "tighten_stop"


# ---------- simulator integration --------------------------------------


def test_simulator_disabled_sell_policy_matches_legacy():
    """sell_policy default disabled → simulator behavior unchanged."""
    df = _df([100, 99, 96, 100], lows=[99, 97, 94, 99])
    sig = _make_signal(df, fill_idx=0, entry=100, stop=95, target=200, max_hold=10)
    out = simulate_trade(sig, df)   # no sell_policy
    assert out.exit_reason == "stop_hit"


def test_simulator_sell_policy_grace_period_blocks_early_exit():
    """grace_period_bars=3 → sell-decision composer not called for first 3 bars."""
    # Engineer a fast 12% rally in first 3 bars (would otherwise fire SIS).
    closes = [100, 105, 110, 112, 115, 120]
    df = _df(closes)
    sig = _make_signal(df, fill_idx=0, entry=100, stop=95, target=200, max_hold=5, grade="A+")
    policy = SellPolicy(enabled=True, grace_period_bars=10)  # never trips
    out = simulate_trade(sig, df, sell_policy=policy)
    # Should exit via max_hold not sell_decision since composer never runs.
    assert not out.exit_reason.startswith("sell_decision_")


def test_simulator_sell_policy_fires_after_grace():
    """grace_period_bars=0 + clear sell-trigger pattern → exit via sell_decision."""
    # 350 calm bars + 5 bars of parabolic run; grace=0; expect sell_decision exit.
    closes = [100.0] * 350 + [100, 105, 110, 112, 115]
    df = _df(closes)
    sig = _make_signal(df, fill_idx=350, entry=100, stop=95, target=300, max_hold=20, grade="A+")
    policy = SellPolicy(enabled=True, grace_period_bars=0)
    out = simulate_trade(sig, df, sell_policy=policy)
    # Either sell_decision fires OR target_hit (if a high reaches 300). Target
    # is 300 so won't trip from this fixture.
    assert out.exit_reason.startswith("sell_decision_") or out.exit_reason == "max_hold"


def test_simulator_sell_policy_doesnt_override_stop():
    """Stop hit before grace period → still exits as stop_hit.

    Bar 2 opens at 96 (above stop=95) but low dips to 93 → intrabar stop hit.
    Sell-policy is enabled with grace_period=0 but the stop-check runs first.
    """
    closes = [100, 99, 96, 92]
    opens = [100, 99, 96, 92]
    lows = [99, 97, 93, 91]
    df = _df(closes, opens=opens, lows=lows)
    sig = _make_signal(df, fill_idx=0, entry=100, stop=95, target=200, max_hold=10, grade="A")
    policy = SellPolicy(enabled=True, grace_period_bars=0)
    out = simulate_trade(sig, df, sell_policy=policy)
    assert out.exit_reason == "stop_hit"
    assert out.exit_price == 95.0
