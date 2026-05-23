"""Tests for tools.backtest.simulator."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from tools.backtest.setup_replay import TradeSignal
from tools.backtest.simulator import simulate_trade, simulate_signals


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


def _make_signal(
    df: pd.DataFrame, fill_idx: int, *, entry=100.0, stop=95.0, target=110.0,
    max_hold=10, grade="A"
):
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


def test_target_hit_exits_at_target():
    """Closes climb steadily; the high on bar 3 reaches 110 → target_hit."""
    closes = [100, 102, 105, 108, 111, 115]
    highs = [101, 103, 106, 109, 112, 116]
    df = _df(closes, highs=highs)
    sig = _make_signal(df, fill_idx=0, entry=100.0, stop=95.0, target=110.0, max_hold=10)
    out = simulate_trade(sig, df)
    assert out.exit_reason == "target_hit"
    assert out.exit_price == 110.0
    assert out.r_multiple == pytest.approx(2.0)


def test_stop_hit_exits_at_stop():
    """Bar 2 dips to 94 → stop hit at 95.0 (stop wins intrabar)."""
    closes = [100, 99, 96, 100]
    lows = [99, 97, 94, 99]
    df = _df(closes, lows=lows)
    sig = _make_signal(df, fill_idx=0, entry=100.0, stop=95.0, target=110.0)
    out = simulate_trade(sig, df)
    assert out.exit_reason == "stop_hit"
    assert out.exit_price == 95.0
    assert out.r_multiple == pytest.approx(-1.0)


def test_gap_through_stop_exits_at_open():
    """Bar 1 gaps down to 90 (open below stop) → exits at the gap open."""
    closes = [100, 89, 88]
    opens = [100, 90, 89]
    df = _df(closes, opens=opens, lows=[c - 0.5 for c in closes])
    sig = _make_signal(df, fill_idx=0, entry=100.0, stop=95.0, target=110.0)
    out = simulate_trade(sig, df)
    assert out.exit_reason == "gap_through_stop"
    assert out.exit_price == 90.0


def test_max_hold_exits_at_close():
    """Neither stop nor target trigger within max_hold → exit at close on bar N."""
    closes = [100, 101, 102, 103, 104]
    df = _df(closes)
    sig = _make_signal(df, fill_idx=0, entry=100.0, stop=95.0, target=120.0, max_hold=4)
    out = simulate_trade(sig, df)
    assert out.exit_reason == "max_hold"
    assert out.exit_price == 104.0
    assert out.bars_held == 4


def test_end_of_data_exits_at_last_close():
    """Less data than max_hold → exit at last close."""
    closes = [100, 101, 102]
    df = _df(closes)
    sig = _make_signal(df, fill_idx=0, entry=100.0, stop=95.0, target=120.0, max_hold=10)
    out = simulate_trade(sig, df)
    assert out.exit_reason == "end_of_data"
    assert out.exit_price == 102.0


def test_invalid_stop_raises():
    df = _df([100, 101, 102])
    sig = _make_signal(df, fill_idx=0, entry=100.0, stop=105.0, target=120.0)
    with pytest.raises(ValueError, match="stop must be below entry"):
        simulate_trade(sig, df)


def test_fill_date_not_in_index_raises():
    df = _df([100, 101, 102])
    sig = TradeSignal(
        ticker="TEST",
        setup_type="SEPA-VCP",
        setup_grade="A",
        entry_date=date(2099, 1, 1),
        fill_date=date(2099, 1, 2),
        entry_price=100,
        stop_price=95,
        target_price=110,
        max_hold_days=10,
        atr_at_signal=2.0,
    )
    with pytest.raises(ValueError, match="fill_date"):
        simulate_trade(sig, df)


def test_simulate_signals_round_trips():
    df = _df([100, 102, 105, 108, 111, 115], highs=[101, 103, 106, 109, 112, 116])
    sig1 = _make_signal(df, fill_idx=0, entry=100, stop=95, target=110)
    sig2 = _make_signal(df, fill_idx=1, entry=102, stop=97, target=112)
    outs = simulate_signals([sig1, sig2], df)
    assert len(outs) == 2
    assert all(o.exit_reason in {"target_hit", "stop_hit", "max_hold", "end_of_data", "gap_through_stop"} for o in outs)
