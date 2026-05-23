"""Tests for tools.backtest.metrics."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tools.backtest.setup_replay import TradeSignal
from tools.backtest.simulator import TradeOutcome
from tools.backtest.metrics import evaluate


def _outcome(r_multiple: float, *, day_offset: int = 0, grade: str = "A",
             bars_held: int = 5) -> TradeOutcome:
    """Construct a synthetic outcome with a controllable r_multiple."""
    fill_date = date(2024, 1, 1) + timedelta(days=day_offset)
    sig = TradeSignal(
        ticker="TEST",
        setup_type="SEPA-VCP",
        setup_grade=grade,
        entry_date=fill_date,
        fill_date=fill_date,
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        max_hold_days=10,
        atr_at_signal=2.5,
    )
    risk = 5.0  # entry - stop
    exit_price = 100.0 + r_multiple * risk
    return TradeOutcome(
        signal=sig,
        exit_date=fill_date + timedelta(days=bars_held),
        exit_price=exit_price,
        exit_reason="target_hit" if r_multiple > 0 else "stop_hit",
        bars_held=bars_held,
        pnl_pct=r_multiple * 0.05,
        r_multiple=r_multiple,
        final_stop=95.0,
    )


def test_empty_outcomes_return_zeros():
    r = evaluate([])
    assert r.trades.n_trades == 0
    assert r.trades.win_rate == 0
    assert r.deployment_gate_passed is False


def test_all_winners_high_win_rate():
    outcomes = [_outcome(2.0, day_offset=i * 7) for i in range(40)]
    r = evaluate(outcomes)
    assert r.trades.n_trades == 40
    assert r.trades.n_wins == 40
    assert r.trades.win_rate == 1.0
    assert r.trades.expectancy_r == pytest.approx(2.0)
    assert r.trades.profit_factor == float("inf")
    assert r.returns.max_drawdown_pct == 0.0  # never down


def test_mixed_outcomes_metrics():
    """50/50 win/loss, +2R wins, -1R losses → expectancy +0.5R, win rate 50%."""
    outcomes = []
    for i in range(40):
        r = 2.0 if i % 2 == 0 else -1.0
        outcomes.append(_outcome(r, day_offset=i * 7))
    r = evaluate(outcomes)
    assert r.trades.win_rate == pytest.approx(0.5)
    assert r.trades.expectancy_r == pytest.approx(0.5)
    assert r.trades.avg_winner_r == pytest.approx(2.0)
    assert r.trades.avg_loser_r == pytest.approx(-1.0)
    assert r.trades.profit_factor == pytest.approx(2.0)


def test_deployment_gate_fails_with_small_n():
    outcomes = [_outcome(2.0, day_offset=i) for i in range(10)]
    r = evaluate(outcomes)
    assert r.deployment_gate_passed is False
    assert "sample size" in r.note


def test_max_drawdown_negative_when_losses_streak():
    """Long losing streak after early gains → noticeable drawdown."""
    outcomes = []
    for i in range(15):
        outcomes.append(_outcome(2.0, day_offset=i * 7))
    for i in range(20):
        outcomes.append(_outcome(-1.0, day_offset=(15 + i) * 7))
    r = evaluate(outcomes)
    assert r.returns.max_drawdown_pct < 0


def test_by_setup_grade_breakdown():
    a_trades = [_outcome(2.0, day_offset=i * 7, grade="A") for i in range(10)]
    b_trades = [_outcome(-1.0, day_offset=(10 + i) * 7, grade="B") for i in range(10)]
    r = evaluate(a_trades + b_trades)
    assert "A" in r.by_setup_grade
    assert "B" in r.by_setup_grade
    assert r.by_setup_grade["A"].win_rate == 1.0
    assert r.by_setup_grade["B"].win_rate == 0.0


def test_by_exit_reason_count():
    outcomes = [_outcome(2.0, day_offset=i * 7) for i in range(5)]
    outcomes += [_outcome(-1.0, day_offset=(5 + i) * 7) for i in range(5)]
    r = evaluate(outcomes)
    assert r.by_exit_reason["target_hit"] == 5
    assert r.by_exit_reason["stop_hit"] == 5


def test_max_consecutive_losses():
    outcomes = []
    # 3W, 5L, 2W, 1L
    pattern = [2.0, 2.0, 2.0, -1.0, -1.0, -1.0, -1.0, -1.0, 2.0, 2.0, -1.0]
    for i, r in enumerate(pattern):
        outcomes.append(_outcome(r, day_offset=i * 7))
    r = evaluate(outcomes)
    assert r.risk.max_consecutive_losses == 5
    assert r.risk.max_consecutive_wins == 3
