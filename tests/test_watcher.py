"""Tests for the entry-trigger watcher core (evaluate + state dedup)."""
from __future__ import annotations

import pandas as pd

from tools.watcher.evaluate import (
    STATUS_APPROACHING,
    STATUS_BLOCKED,
    STATUS_FIRED,
    STATUS_INACTIVE,
    STATUS_IN_ZONE,
    WatchSignal,
    evaluate_entry,
)
from tools.watcher.state import decide_pushes


def _df(last_close, *, base=114.0, n=30, candle="plain", vol_last=1.0, vol_avg=1.0):
    """Build a synthetic OHLCV frame; control the last bar's candle + volume."""
    o = [base] * n
    h = [base * 1.004] * n
    lo = [base * 0.996] * n
    c = [base] * (n - 1) + [last_close]
    v = [vol_avg * 1e6] * (n - 1) + [vol_last * 1e6]
    if candle == "engulf":  # prior red, today green body engulfs prior
        o[-2], c[-2] = last_close - 1.0, last_close - 2.5
        o[-1] = last_close - 3.0
        h[-1] = last_close * 1.002
        lo[-1] = (last_close - 3.0) * 0.999
    else:  # plain: prior green, today small green — no reversal pattern
        o[-2], c[-2] = last_close - 1.5, last_close - 0.5
        o[-1] = last_close - 0.4
        h[-1] = last_close * 1.003
        lo[-1] = last_close * 0.997
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c, "Volume": v}, index=idx)


PB = {"type": "pullback_zone", "zone_low": 114.0, "zone_high": 117.0,
      "require_reversal_candle": True, "entry": 115.5, "stop": 108.0, "target": 130.0}
BO = {"type": "breakout", "level": 130.37, "min_volume_mult": 1.4,
      "entry": 130.5, "stop": 121.0, "target": 145.0}


# ---- pullback_zone ----

def test_pullback_in_zone_with_candle_fires():
    s = evaluate_entry("CSCO", PB, _df(115.5, candle="engulf"), regime_ok=True)
    assert s.status == STATUS_FIRED
    assert s.payload["target"] == 130.0


def test_pullback_in_zone_without_candle_is_in_zone():
    s = evaluate_entry("CSCO", PB, _df(115.5, candle="plain"), regime_ok=True)
    assert s.status == STATUS_IN_ZONE


def test_pullback_in_zone_blocked_by_regime():
    s = evaluate_entry("CSCO", PB, _df(115.5, candle="engulf"), regime_ok=False)
    assert s.status == STATUS_BLOCKED


def test_pullback_just_above_zone_is_approaching():
    # zone_high 117; 2% above = 119.34 (within the 3% approaching band)
    s = evaluate_entry("CSCO", PB, _df(119.0), regime_ok=True)
    assert s.status == STATUS_APPROACHING


def test_pullback_far_above_zone_is_inactive():
    s = evaluate_entry("CSCO", PB, _df(130.0), regime_ok=True)
    assert s.status == STATUS_INACTIVE


def test_pullback_overshoot_below_zone_is_inactive_flagged():
    s = evaluate_entry("CSCO", PB, _df(110.0), regime_ok=True)
    assert s.status == STATUS_INACTIVE
    assert s.detail.get("overshoot") is True


# ---- breakout ----

def test_breakout_above_level_on_volume_fires():
    s = evaluate_entry("X", BO, _df(131.0, base=128.0, vol_last=1.6, vol_avg=1.0), regime_ok=True)
    assert s.status == STATUS_FIRED
    assert s.detail["vol_ratio"] >= 1.4


def test_breakout_above_level_low_volume_is_in_zone():
    s = evaluate_entry("X", BO, _df(131.0, base=128.0, vol_last=0.8, vol_avg=1.0), regime_ok=True)
    assert s.status == STATUS_IN_ZONE


def test_breakout_below_level_far_is_inactive():
    s = evaluate_entry("X", BO, _df(120.0, base=120.0), regime_ok=True)
    assert s.status == STATUS_INACTIVE


def test_breakout_just_below_level_is_approaching():
    s = evaluate_entry("X", BO, _df(128.0, base=128.0), regime_ok=True)  # ~1.8% below 130.37
    assert s.status == STATUS_APPROACHING


def test_breakout_above_level_blocked_by_regime():
    s = evaluate_entry("X", BO, _df(131.0, base=128.0, vol_last=2.0), regime_ok=False)
    assert s.status == STATUS_BLOCKED


# ---- state dedup ----

def _sig(ticker, status):
    return WatchSignal(ticker=ticker, status=status, price=100.0,
                       trigger_type="pullback_zone", distance_pct=0.0, reason="")


def test_escalation_pushes_once_then_suppresses():
    state = {}
    push, state = decide_pushes([_sig("A", STATUS_APPROACHING)], state, today_key="2026-06-24")
    assert [s.ticker for s in push] == ["A"]
    # same status next tick → suppressed
    push, state = decide_pushes([_sig("A", STATUS_APPROACHING)], state, today_key="2026-06-24")
    assert push == []
    # escalates to in_zone → push
    push, state = decide_pushes([_sig("A", STATUS_IN_ZONE)], state, today_key="2026-06-24")
    assert [s.ticker for s in push] == ["A"]


def test_fired_repages_next_day_not_same_day():
    state = {}
    push, state = decide_pushes([_sig("A", STATUS_FIRED)], state, today_key="2026-06-24")
    assert len(push) == 1
    push, state = decide_pushes([_sig("A", STATUS_FIRED)], state, today_key="2026-06-24")
    assert push == []  # same day, already fired
    push, state = decide_pushes([_sig("A", STATUS_FIRED)], state, today_key="2026-06-25")
    assert len(push) == 1  # next day re-page


def test_inactive_does_not_push():
    state = {}
    push, state = decide_pushes([_sig("A", STATUS_INACTIVE)], state, today_key="2026-06-24")
    assert push == []


def test_deescalation_resets_for_refire():
    state = {}
    push, state = decide_pushes([_sig("A", STATUS_FIRED)], state, today_key="2026-06-24")
    assert len(push) == 1
    # moves away → inactive (no push, state records it)
    push, state = decide_pushes([_sig("A", STATUS_INACTIVE)], state, today_key="2026-06-24")
    assert push == []
    # comes back to fired → pushes again (escalation from inactive)
    push, state = decide_pushes([_sig("A", STATUS_FIRED)], state, today_key="2026-06-24")
    assert len(push) == 1
