"""Tests for the Phase 5.b setup-replay modules (EP + 3 secondaries).

These tests focus on shape + registration. Validating that the replays
FIRE on engineered synthetic data is a larger fixture-design exercise
deferred to a later round; here we verify each module:

* registers itself in SETUP_REPLAY_REGISTRY
* exposes a replay function with the right signature
* handles insufficient-history input cleanly (empty list, no crash)
* validates required OHLCV columns
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

# Importing setup_replay triggers side-effect imports for all replay modules.
from tools.backtest.setup_replay import SETUP_REPLAY_REGISTRY


def _short_df(n: int = 30) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    closes = np.array([100.0 + i * 0.1 for i in range(n)])
    opens = closes
    highs = closes + 0.5
    lows = closes - 0.5
    volumes = rng.integers(900_000, 1_100_000, size=n).astype(int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2024-01-02", periods=n, freq="B"),
    )


def _long_df(n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(2)
    closes = np.array([100.0 + i * 0.1 for i in range(n)])
    opens = closes
    highs = closes + 0.5
    lows = closes - 0.5
    volumes = rng.integers(900_000, 1_100_000, size=n).astype(int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2023-01-02", periods=n, freq="B"),
    )


def test_registry_has_all_phase5b_setups():
    for setup in ("SEPA-VCP", "EP", "Pullback-20SMA", "RSI-Divergence", "Resistance-Breakout"):
        assert setup in SETUP_REPLAY_REGISTRY


@pytest.mark.parametrize(
    "setup_name",
    ["EP", "Pullback-20SMA", "RSI-Divergence", "Resistance-Breakout"],
)
def test_replay_returns_list_on_insufficient_history(setup_name):
    """Short df with not enough history → empty signal list (no crash)."""
    fn = SETUP_REPLAY_REGISTRY[setup_name]
    df = _short_df(n=30)
    signals = fn(df, ticker="TEST")
    assert signals == []


@pytest.mark.parametrize(
    "setup_name",
    ["EP", "Pullback-20SMA", "RSI-Divergence", "Resistance-Breakout"],
)
def test_replay_validates_required_columns(setup_name):
    fn = SETUP_REPLAY_REGISTRY[setup_name]
    df = pd.DataFrame({"Close": [100.0] * 50},
                      index=pd.date_range("2024-01-02", periods=50, freq="B"))
    with pytest.raises(ValueError, match="missing"):
        fn(df, ticker="TEST")


@pytest.mark.parametrize(
    "setup_name",
    ["EP", "Pullback-20SMA", "RSI-Divergence", "Resistance-Breakout"],
)
def test_replay_returns_list_shape_for_long_df(setup_name):
    """Long synthetic df may or may not produce signals; returned shape
    must always be a list of TradeSignal."""
    fn = SETUP_REPLAY_REGISTRY[setup_name]
    df = _long_df(n=400)
    signals = fn(df, ticker="TEST")
    assert isinstance(signals, list)
    for s in signals:
        assert s.ticker == "TEST"
        assert s.entry_price > 0
        assert s.stop_price > 0
        assert s.stop_price < s.entry_price
        assert s.target_price is None or s.target_price > s.entry_price
        assert s.max_hold_days > 0


def test_ep_replay_emits_ep_setup_type():
    """Even if the synthetic df doesn't fire, the FUNCTION's contract
    must label EP signals correctly. Use a hand-shaped gap fixture."""
    n = 250
    rng = np.random.default_rng(3)
    closes = np.array([50.0 + i * 0.05 for i in range(n)])  # slow uptrend (neglected)
    opens = closes.copy()
    highs = closes + 0.3
    lows = closes - 0.3
    volumes = rng.integers(900_000, 1_100_000, size=n).astype(int)

    # Engineer the last bar: gap up 14%, close above open, big volume.
    closes[-1] = closes[-2] * 1.14
    opens[-1] = closes[-2] * 1.13  # gap up
    highs[-1] = closes[-1] * 1.02
    lows[-1] = opens[-1] * 0.99
    volumes[-1] = volumes[-2] * 5  # 5x ADV

    df = pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2023-01-02", periods=n, freq="B"),
    )

    fn = SETUP_REPLAY_REGISTRY["EP"]
    signals = fn(df, ticker="EPTEST", start_index=200)
    # With this fixture, may or may not fire (depends on neglected check
    # over the full 6m window — which is in slow uptrend so rally_6m may
    # be just above threshold). If any signal fires, verify shape:
    for s in signals:
        assert s.setup_type == "EP"
        assert s.setup_grade in {"Swan", "SuperSwan", "GoldenEP"}
