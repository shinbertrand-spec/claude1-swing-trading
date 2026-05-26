"""Tests for tools.backtest.metrics."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tools.backtest.setup_replay import TradeSignal
from tools.backtest.simulator import TradeOutcome
from tools.backtest.metrics import (
    DEFAULT_MAX_CONCURRENT,
    BacktestReport,
    ReturnStats,
    RiskStats,
    TradeStats,
    _apply_concurrent_cap,
    evaluate,
    evaluate_aggregated_with_windows,
)


def _synthetic_report(
    *,
    sharpe: float,
    max_dd_pct: float = -10.0,
    n_trades: int = 50,
) -> BacktestReport:
    """Build a BacktestReport with controllable gate-relevant fields.

    Bypasses :func:`evaluate` so tests can drive the gate without
    constructing realistic outcome sequences for every Sharpe/DD/n combo.
    Only the fields read by :func:`evaluate_aggregated_with_windows` are
    meaningful — the rest are filled with neutral zeros.
    """
    trades = TradeStats(
        n_trades=n_trades, n_wins=0, n_losses=0, n_breakeven=0,
        win_rate=0.0, avg_winner_r=0.0, avg_loser_r=0.0,
        expectancy_r=0.0, profit_factor=0.0, avg_bars_held=0.0,
    )
    risk = RiskStats(
        total_r=0.0, max_consecutive_losses=0, max_consecutive_wins=0,
        largest_winner_r=0.0, largest_loser_r=0.0,
    )
    returns = ReturnStats(
        sharpe_annualised=sharpe,
        sortino_annualised=0.0,
        calmar=0.0,
        max_drawdown_pct=max_dd_pct,
        cumulative_return_pct=0.0,
        cagr_pct=0.0,
    )
    # The per-flat-list gate uses Sharpe > 1.0 AND |DD| < 25 AND n >= 30.
    flat_gate = sharpe > 1.0 and abs(max_dd_pct) < 25.0 and n_trades >= 30
    return BacktestReport(
        trades=trades, risk=risk, returns=returns,
        deployment_gate_passed=flat_gate,
        by_exit_reason={}, by_setup_grade={},
    )


def _outcome(r_multiple: float, *, day_offset: int = 0, grade: str = "A",
             bars_held: int = 5, ticker: str = "TEST") -> TradeOutcome:
    """Construct a synthetic outcome with a controllable r_multiple."""
    fill_date = date(2024, 1, 1) + timedelta(days=day_offset)
    sig = TradeSignal(
        ticker=ticker,
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


# ---------------------------------------------------------------------------
# Tightened deployment gate — evaluate_aggregated_with_windows
# ---------------------------------------------------------------------------
#
# The doctrine gate composes two clauses:
#   1. Aggregate-OOS clause (original): Sharpe > 1.0 AND |MDD| < 25% AND n >= 30
#   2. Per-window clause (new):         >= 50% of windows individually Sharpe > 0.5
#
# Both must pass. The per-window clause catches the SEPA-VCP case where one
# outlier year (2024 Sharpe 4.27) carries an otherwise-fragile aggregate
# (2023 -0.44, 2025 -1.38).


def test_tightened_gate_both_clauses_pass():
    """Aggregate passes AND >=half of windows clear the floor → gate passes."""
    agg = _synthetic_report(sharpe=2.0, max_dd_pct=-10.0, n_trades=100)
    windows = [
        _synthetic_report(sharpe=1.5),
        _synthetic_report(sharpe=2.0),
        _synthetic_report(sharpe=1.8),
    ]
    result = evaluate_aggregated_with_windows(agg, windows)
    assert result.aggregate_gate_passed is True
    assert result.window_clause_passed is True
    assert result.window_pass_rate == 1.0
    assert result.gate_passed is True
    assert result.note == ""


def test_tightened_gate_aggregate_pass_per_window_fail():
    """SEPA-VCP-style case: aggregate passes (1.37) but 2 of 3 windows fail.

    Per-window Sharpes -0.44 / 4.27 / -1.38 — only 1/3 clears the 0.5 floor.
    The tightened gate must reject this case. This is the central reason the
    per-window clause exists.
    """
    agg = _synthetic_report(sharpe=1.37, max_dd_pct=-15.95, n_trades=493)
    windows = [
        _synthetic_report(sharpe=-0.44),
        _synthetic_report(sharpe=4.27),
        _synthetic_report(sharpe=-1.38),
    ]
    result = evaluate_aggregated_with_windows(agg, windows)
    assert result.aggregate_gate_passed is True
    assert result.window_clause_passed is False
    assert result.n_windows_above_floor == 1
    assert result.window_pass_rate == pytest.approx(1 / 3)
    assert result.gate_passed is False
    assert "windows have Sharpe" in result.note


def test_tightened_gate_aggregate_fail_blocks_regardless():
    """If the aggregate clause fails, the composite gate fails even if all
    windows individually pass. Defends against an aggregate that lost too much
    (DD breach) being rescued by a misleading per-window pass rate."""
    agg = _synthetic_report(sharpe=2.0, max_dd_pct=-30.0, n_trades=100)
    windows = [
        _synthetic_report(sharpe=2.0),
        _synthetic_report(sharpe=2.0),
        _synthetic_report(sharpe=2.0),
    ]
    result = evaluate_aggregated_with_windows(agg, windows)
    assert result.aggregate_gate_passed is False
    assert result.window_clause_passed is True
    assert result.gate_passed is False
    assert "aggregate fails" in result.note
    assert "MDD" in result.note


def test_tightened_gate_exactly_half_window_pass_rate_passes():
    """4 windows, 2 above floor → pass rate exactly 0.5 → window clause passes
    (the threshold is >=, not >)."""
    agg = _synthetic_report(sharpe=1.5, max_dd_pct=-10.0, n_trades=80)
    windows = [
        _synthetic_report(sharpe=1.5),
        _synthetic_report(sharpe=1.0),
        _synthetic_report(sharpe=0.3),
        _synthetic_report(sharpe=-0.5),
    ]
    result = evaluate_aggregated_with_windows(agg, windows)
    assert result.n_windows_above_floor == 2
    assert result.window_pass_rate == 0.5
    assert result.window_clause_passed is True
    assert result.gate_passed is True


def test_tightened_gate_clenow_2025_at_floor_boundary():
    """Clenow's 2025 OOS Sharpe is 0.59 — barely above the 0.5 floor.
    With aggregate Sharpe 2.02 + 3/3 windows clearing floor, gate passes.
    This documents the live edge: lowering the floor to 0.6 would knock Clenow
    out, which is exactly the sensitivity intuition the gate is meant to surface."""
    agg = _synthetic_report(sharpe=2.02, max_dd_pct=-22.57, n_trades=1360)
    windows = [
        _synthetic_report(sharpe=2.64),
        _synthetic_report(sharpe=2.70),
        _synthetic_report(sharpe=0.59),
    ]
    result = evaluate_aggregated_with_windows(agg, windows)
    assert result.n_windows_above_floor == 3
    assert result.gate_passed is True

    # Same data, floor raised to 0.6 — knocks out 2025.
    stricter = evaluate_aggregated_with_windows(
        agg, windows, min_window_sharpe=0.6,
    )
    assert stricter.n_windows_above_floor == 2
    assert stricter.gate_passed is True  # 2/3 = 0.67 still >= 0.5

    # Floor 0.6 AND pass-rate requirement raised to 0.75 → fails.
    even_stricter = evaluate_aggregated_with_windows(
        agg, windows, min_window_sharpe=0.6, min_window_pass_rate=0.75,
    )
    assert even_stricter.gate_passed is False


def test_tightened_gate_no_windows_supplied_fails_gracefully():
    """If no per-window reports are supplied, the window clause cannot be
    evaluated and the gate must fail (no false PASSes)."""
    agg = _synthetic_report(sharpe=2.0, max_dd_pct=-10.0, n_trades=100)
    result = evaluate_aggregated_with_windows(agg, [])
    assert result.n_windows == 0
    assert result.window_clause_passed is False
    assert result.gate_passed is False
    assert "no per-window OOS reports" in result.note


def test_tightened_gate_thresholds_are_overridable():
    """Spec gates may override doctrine defaults — e.g. quant-strategies YAMLs
    can require Sharpe > 1.5 instead of 1.0."""
    agg = _synthetic_report(sharpe=1.2, max_dd_pct=-10.0, n_trades=100)
    windows = [_synthetic_report(sharpe=1.5) for _ in range(3)]

    # Default (Sharpe > 1.0): passes.
    default = evaluate_aggregated_with_windows(agg, windows)
    assert default.gate_passed is True

    # Tighter (Sharpe > 1.5): fails aggregate clause.
    stricter = evaluate_aggregated_with_windows(agg, windows, sharpe_min=1.5)
    assert stricter.aggregate_gate_passed is False
    assert stricter.gate_passed is False


# ---------------------------------------------------------------------------
# Concurrent-position cap (2026-05-26 simulator concurrency fix)
# ---------------------------------------------------------------------------
#
# The simulator concurrency bug: _equity_curve compounded trades in list
# order without modelling overlap. On a wide universe (sp500_2026q2,
# 503 tickers) many trades overlap in time but the equity math treated
# them as sequential bets — produced Sharpe 3.02 + DD -70.62% on dual_ma,
# which is mathematically inconsistent.
#
# Fix: _apply_concurrent_cap walks outcomes in fill_date order, tracks
# still-open positions, and drops any trade that would exceed the cap.
# Default cap = 8 (CLAUDE.md hard rule).


def test_concurrent_cap_drops_overlapping_trades():
    """10 trades all fill on day 1, hold 5 days. cap=8 → 2 dropped."""
    outcomes = [
        _outcome(2.0, day_offset=0, ticker=f"T{i:02d}", bars_held=5)
        for i in range(10)
    ]
    accepted = _apply_concurrent_cap(outcomes, max_concurrent=8)
    assert len(accepted) == 8
    # Ticker tie-break preserves the first 8 by lexical (T00..T07).
    assert {o.signal.ticker for o in accepted} == {f"T{i:02d}" for i in range(8)}


def test_concurrent_cap_keeps_sequential_trades():
    """10 trades spaced 7 days apart with 5-day hold — no overlap. All kept."""
    outcomes = [
        _outcome(2.0, day_offset=i * 7, ticker=f"T{i:02d}", bars_held=5)
        for i in range(10)
    ]
    accepted = _apply_concurrent_cap(outcomes, max_concurrent=8)
    assert len(accepted) == 10


def test_concurrent_cap_none_disables():
    """max_concurrent=None recovers pre-fix behavior — no filtering."""
    outcomes = [
        _outcome(2.0, day_offset=0, ticker=f"T{i:02d}", bars_held=5)
        for i in range(10)
    ]
    accepted = _apply_concurrent_cap(outcomes, max_concurrent=None)
    assert len(accepted) == 10


def test_concurrent_cap_zero_disables():
    """max_concurrent<=0 disables — caller can pass 0 to opt out via CLI."""
    outcomes = [
        _outcome(2.0, day_offset=0, ticker=f"T{i:02d}", bars_held=5)
        for i in range(10)
    ]
    accepted = _apply_concurrent_cap(outcomes, max_concurrent=0)
    assert len(accepted) == 10


def test_concurrent_cap_same_day_exit_frees_slot():
    """Trade A exits on day D; trade B fills on day D. B accepted (slot freed).

    Same-day handoff is permissive: cash from the exit is available before
    the new entry order is placed on liquid US equities (T+0 settlement
    convention in this model).
    """
    # 8 trades fill on day 0, hold 5 days (exit on day 5).
    # 1 trade fills on day 5 — should be accepted, the 8 are closing today.
    early = [
        _outcome(2.0, day_offset=0, ticker=f"E{i:02d}", bars_held=5)
        for i in range(8)
    ]
    late = _outcome(1.0, day_offset=5, ticker="LATE", bars_held=5)
    accepted = _apply_concurrent_cap(early + [late], max_concurrent=8)
    assert len(accepted) == 9
    assert any(o.signal.ticker == "LATE" for o in accepted)


def test_concurrent_cap_partial_overlap():
    """Stagger 12 trades 1 day apart, hold 10 days. cap=8 → drops the 4 that
    would have made the open count exceed 8 at their respective fill_dates."""
    outcomes = [
        _outcome(2.0, day_offset=i, ticker=f"T{i:02d}", bars_held=10)
        for i in range(12)
    ]
    accepted = _apply_concurrent_cap(outcomes, max_concurrent=8)
    # Trades at day_offset 0..7 fill before any exit (first exit on day 10).
    # Trades at day_offset 8, 9 fill before first exit — also rejected.
    # Trade at day_offset 10 fills the same day T00 exits — accepted (slot freed).
    # The exact count depends on the exit-slot freeing: by the time T10 fills,
    # T00 has just exited so the slot is open. Same for T11 (T01 just exited).
    # So accepted: T00..T07 (8) + T10 (T00's slot freed) + T11 (T01's slot freed) = 10.
    assert len(accepted) == 10
    accepted_tickers = {o.signal.ticker for o in accepted}
    assert "T08" not in accepted_tickers
    assert "T09" not in accepted_tickers


def test_evaluate_applies_default_cap():
    """evaluate() defaults to max_concurrent=DEFAULT_MAX_CONCURRENT (8)."""
    assert DEFAULT_MAX_CONCURRENT == 8
    outcomes = [
        _outcome(2.0, day_offset=0, ticker=f"T{i:02d}", bars_held=5)
        for i in range(10)
    ]
    r = evaluate(outcomes)
    # 8 trades pass the cap. n_trades reflects post-cap count.
    assert r.trades.n_trades == 8
    assert "concurrent-position cap" in r.note
    assert "rejected 2/10" in r.note


def test_evaluate_max_concurrent_none_preserves_all():
    """Pass max_concurrent=None to recover pre-fix behavior."""
    outcomes = [
        _outcome(2.0, day_offset=0, ticker=f"T{i:02d}", bars_held=5)
        for i in range(10)
    ]
    r = evaluate(outcomes, max_concurrent=None)
    assert r.trades.n_trades == 10
    assert "concurrent-position cap" not in r.note


def test_evaluate_caps_existing_test_fixtures_unchanged():
    """Existing test fixtures use 7-day-spaced trades with 5-day holds (no
    overlap). Default cap=8 must NOT change their behavior. Smoke check."""
    outcomes = [_outcome(2.0, day_offset=i * 7, ticker=f"S{i:02d}") for i in range(40)]
    r_default = evaluate(outcomes)
    r_uncapped = evaluate(outcomes, max_concurrent=None)
    assert r_default.trades.n_trades == r_uncapped.trades.n_trades == 40
    assert r_default.returns.sharpe_annualised == r_uncapped.returns.sharpe_annualised


def test_concurrent_cap_with_real_wide_universe_drops_drawdown():
    """100 trades on the same day with -2R losses each — without cap, the
    sequential-compounding equity curve compounds 100 losses; with cap=8,
    only 8 are on book. The DD on the capped version is materially smaller.

    This is the reduced-form version of the dual_ma sp500_2026q2 bug
    (Sharpe 3 + DD -70%); cap=8 produces honest numbers.
    """
    outcomes = [
        _outcome(-2.0, day_offset=0, ticker=f"L{i:03d}", bars_held=5)
        for i in range(100)
    ]
    r_uncapped = evaluate(outcomes, max_concurrent=None)
    r_capped = evaluate(outcomes, max_concurrent=8)
    # Uncapped: 100 sequential 2% losses compound to ~-87% (1 - 0.98^100).
    # Capped: only 8 losses recorded.
    assert r_uncapped.returns.max_drawdown_pct < -50.0  # catastrophic, sequential
    assert r_capped.returns.max_drawdown_pct > -20.0   # bounded, only 8 on book
    assert r_capped.trades.n_trades == 8
    assert r_uncapped.trades.n_trades == 100
