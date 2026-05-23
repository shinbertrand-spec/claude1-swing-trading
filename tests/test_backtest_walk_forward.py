"""Tests for tools.backtest.walk_forward."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from tools.backtest.setup_replay import TradeSignal
from tools.backtest.simulator import TradeOutcome
from tools.backtest.walk_forward import (
    WindowSpec,
    rolling_splits,
    single_split,
    split_trades_by_window,
    trim_ohlcv,
)


def test_single_split_70_30_by_default():
    spec = single_split(date(2020, 1, 1), date(2025, 1, 1))
    span = (date(2025, 1, 1) - date(2020, 1, 1)).days
    expected_is_end = date(2020, 1, 1) + timedelta(days=int(span * 0.70))
    assert spec.in_sample_end == expected_is_end
    assert spec.out_of_sample_end == date(2025, 1, 1)


def test_single_split_rejects_bad_range():
    with pytest.raises(ValueError, match="must be after"):
        single_split(date(2025, 1, 1), date(2020, 1, 1))


def test_rolling_splits_3_year_is_1_year_oos():
    splits = rolling_splits(date(2015, 1, 1), date(2025, 1, 1), is_years=3, oos_years=1, step_years=1)
    # 2015..2025 = 10 years; 3+1=4 windows per ladder; step 1 → 7 windows
    # (2015-18 IS / 2018-19 OOS) ... (2021-24 IS / 2024-25 OOS) = 7 specs.
    assert len(splits) == 7
    assert splits[0].in_sample_start == date(2015, 1, 1)
    assert splits[0].in_sample_end == date(2018, 1, 1)
    assert splits[0].out_of_sample_end == date(2019, 1, 1)
    assert splits[-1].out_of_sample_end == date(2025, 1, 1)


def test_rolling_splits_reject_nonpositive_years():
    with pytest.raises(ValueError, match="must all be positive"):
        rolling_splits(date(2020, 1, 1), date(2025, 1, 1), is_years=0)


def _signal_outcome(d: date) -> tuple[TradeSignal, TradeOutcome]:
    sig = TradeSignal(
        ticker="X", setup_type="SEPA-VCP", setup_grade="A",
        entry_date=d, fill_date=d,
        entry_price=100, stop_price=95, target_price=110,
        max_hold_days=10, atr_at_signal=2.0,
    )
    out = TradeOutcome(
        signal=sig, exit_date=d + timedelta(days=5), exit_price=105,
        exit_reason="max_hold", bars_held=5, pnl_pct=0.05, r_multiple=1.0,
        final_stop=95.0,
    )
    return sig, out


def test_split_partitions_by_fill_date():
    spec = WindowSpec(
        in_sample_start=date(2024, 1, 1),
        in_sample_end=date(2024, 7, 1),
        out_of_sample_end=date(2025, 1, 1),
    )
    pairs = [
        _signal_outcome(date(2023, 12, 15)),  # before IS → dropped
        _signal_outcome(date(2024, 3, 1)),     # IS
        _signal_outcome(date(2024, 6, 30)),    # IS
        _signal_outcome(date(2024, 7, 1)),     # OOS (== is_end)
        _signal_outcome(date(2024, 10, 15)),   # OOS
        _signal_outcome(date(2025, 2, 1)),     # after OOS → dropped
    ]
    sigs = [p[0] for p in pairs]
    outs = [p[1] for p in pairs]
    is_s, is_o, oos_s, oos_o = split_trades_by_window(sigs, outs, spec)
    assert len(is_s) == 2
    assert len(oos_s) == 2
    assert len(is_o) == 2 and len(oos_o) == 2


def test_trim_ohlcv():
    idx = pd.date_range("2024-01-02", periods=10, freq="B")
    df = pd.DataFrame({"Close": list(range(10))}, index=idx)
    trimmed = trim_ohlcv(df, date(2024, 1, 5), date(2024, 1, 10))
    # 2024-01-05 is a Friday (business day) included; 2024-01-10 excluded.
    assert len(trimmed) > 0
    assert trimmed.index[0].date() >= date(2024, 1, 5)
    assert trimmed.index[-1].date() < date(2024, 1, 10)
