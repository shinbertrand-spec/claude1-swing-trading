"""Tests for tools.backtest.setup_replay."""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.backtest.setup_replay import (
    SETUP_REPLAY_REGISTRY,
    _detect_sepa_vcp_at_bar,
    replay_sepa_vcp,
)


def _make_long_uptrend(n: int = 400, start: float = 50.0) -> pd.DataFrame:
    """Synthetic 400-bar (~1.5y) clean uptrend with low noise."""
    rng = np.random.default_rng(91)
    closes = np.array([start + i * 0.30 for i in range(n)], dtype=float)
    noise = rng.normal(0, 0.20, size=n)
    closes = closes + noise
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    highs = closes + 0.5
    lows = closes - 0.5
    volumes = rng.integers(900_000, 1_100_000, size=n).astype(int)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2023-01-02", periods=n, freq="B"),
    )


def test_detect_returns_false_without_history():
    df = _make_long_uptrend(n=100)
    detected, _, evidence = _detect_sepa_vcp_at_bar(df)
    assert detected is False
    assert "insufficient history" in evidence["reason"]


def test_detect_returns_false_in_downtrend():
    n = 400
    rng = np.random.default_rng(89)
    closes = np.array([200.0 - i * 0.10 for i in range(n)], dtype=float)
    opens = closes
    highs = closes + 0.5
    lows = closes - 0.5
    volumes = rng.integers(900_000, 1_100_000, size=n).astype(int)
    df = pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.date_range("2023-01-02", periods=n, freq="B"),
    )
    detected, _, _ = _detect_sepa_vcp_at_bar(df)
    assert detected is False


def test_registry_includes_sepa_vcp():
    assert "SEPA-VCP" in SETUP_REPLAY_REGISTRY
    assert SETUP_REPLAY_REGISTRY["SEPA-VCP"] is replay_sepa_vcp


def test_replay_returns_list_of_signals():
    df = _make_long_uptrend(n=400)
    signals = replay_sepa_vcp(df, ticker="TEST", max_hold_days=20)
    # Replay always returns a list (possibly empty); shape is what matters.
    assert isinstance(signals, list)
    # If signals fire, each one must have the right shape.
    for s in signals:
        assert s.ticker == "TEST"
        assert s.setup_type == "SEPA-VCP"
        assert s.setup_grade in {"A", "B", "C"}
        assert s.entry_price > 0
        assert s.stop_price > 0
        assert s.stop_price < s.entry_price
        assert s.target_price is not None and s.target_price > s.entry_price
        assert s.max_hold_days == 20
        assert s.atr_at_signal > 0


def test_replay_respects_start_index():
    """start_index=10000 (way beyond df length) → no signals possible."""
    df = _make_long_uptrend(n=400)
    signals = replay_sepa_vcp(df, ticker="TEST", start_index=10000)
    assert signals == []


def test_replay_emits_target_at_target_r_multiple():
    """target should = entry + R × (entry - stop) per the function arg."""
    df = _make_long_uptrend(n=400)
    signals = replay_sepa_vcp(df, ticker="TEST", target_r_multiple=3.0)
    for s in signals:
        risk = s.entry_price - s.stop_price
        expected_target = s.entry_price + 3.0 * risk
        assert abs(s.target_price - expected_target) < 1e-9
