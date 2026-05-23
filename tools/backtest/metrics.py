"""Backtest performance metrics.

Per ``walk-forward-analysis`` (vault concept page): Sharpe, Sortino,
Calmar, max drawdown, win rate, profit factor, R-multiple distribution,
per-setup-grade breakdown.

The doctrine's deployment gate:

    Out-of-sample Sharpe > 1.0 AND max drawdown < 25%

If either fails on OOS data, the setup does not ship to live.

This module operates on a list of :class:`TradeOutcome` (one per trade,
sequential in time). Assumes equal-risk-per-trade — R-multiple is the
canonical return unit. For a portfolio-equity view, callers should
convert R-multiples to dollar returns using their per-trade risk budget.

Phase 5.a baseline: trade-level metrics. Portfolio-equity simulation
(multiple concurrent positions, position sizing, cash tracking) deferred
to Phase 5.b.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .simulator import TradeOutcome

TRADING_DAYS_PER_YEAR = 252


@dataclass
class TradeStats:
    n_trades: int
    n_wins: int
    n_losses: int
    n_breakeven: int
    win_rate: float
    avg_winner_r: float
    avg_loser_r: float
    expectancy_r: float           # avg R per trade
    profit_factor: float           # gross profit / |gross loss|
    avg_bars_held: float


@dataclass
class RiskStats:
    total_r: float
    max_consecutive_losses: int
    max_consecutive_wins: int
    largest_winner_r: float
    largest_loser_r: float


@dataclass
class ReturnStats:
    sharpe_annualised: float        # assumes 0 risk-free, 252 trading days
    sortino_annualised: float       # downside-deviation variant
    calmar: float                   # CAGR / |max_drawdown|
    max_drawdown_pct: float
    cumulative_return_pct: float
    cagr_pct: float


@dataclass
class BacktestReport:
    trades: TradeStats
    risk: RiskStats
    returns: ReturnStats
    deployment_gate_passed: bool   # Sharpe > 1.0 AND max_drawdown < 25%
    by_exit_reason: dict[str, int]
    by_setup_grade: dict[str, "TradeStats"]
    note: str = ""


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b != 0 else default


def _consecutive(seq: list[bool]) -> int:
    """Max run of True values in ``seq``."""
    best = current = 0
    for v in seq:
        if v:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _compute_trade_stats(outcomes: list[TradeOutcome]) -> TradeStats:
    if not outcomes:
        return TradeStats(0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    r_values = [o.r_multiple for o in outcomes]
    winners = [r for r in r_values if r > 0]
    losers = [r for r in r_values if r < 0]
    breakeven = sum(1 for r in r_values if r == 0)
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    return TradeStats(
        n_trades=len(outcomes),
        n_wins=len(winners),
        n_losses=len(losers),
        n_breakeven=breakeven,
        win_rate=_safe_div(len(winners), len(outcomes)),
        avg_winner_r=_safe_div(gross_profit, len(winners)),
        avg_loser_r=_safe_div(sum(losers), len(losers)),
        expectancy_r=_safe_div(sum(r_values), len(outcomes)),
        profit_factor=_safe_div(gross_profit, gross_loss, default=float("inf") if gross_profit > 0 else 0.0),
        avg_bars_held=_safe_div(sum(o.bars_held for o in outcomes), len(outcomes)),
    )


def _compute_risk_stats(outcomes: list[TradeOutcome]) -> RiskStats:
    if not outcomes:
        return RiskStats(0.0, 0, 0, 0.0, 0.0)
    r_values = [o.r_multiple for o in outcomes]
    return RiskStats(
        total_r=sum(r_values),
        max_consecutive_losses=_consecutive([r < 0 for r in r_values]),
        max_consecutive_wins=_consecutive([r > 0 for r in r_values]),
        largest_winner_r=max(r_values),
        largest_loser_r=min(r_values),
    )


def _equity_curve(outcomes: list[TradeOutcome], risk_per_trade: float = 0.01) -> np.ndarray:
    """Convert R-multiples to a cumulative-return series.

    Each trade contributes ``risk_per_trade × r_multiple`` to portfolio return.
    Default risk_per_trade = 0.01 (1% account risk) — matches the lower end
    of swing-position-sizing's risk budget.
    """
    trade_returns = np.array([risk_per_trade * o.r_multiple for o in outcomes])
    # Geometric compounding.
    return np.cumprod(1 + trade_returns)


def _compute_return_stats(
    outcomes: list[TradeOutcome],
    risk_per_trade: float = 0.01,
) -> ReturnStats:
    if not outcomes:
        return ReturnStats(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    trade_returns = np.array([risk_per_trade * o.r_multiple for o in outcomes])
    n = len(trade_returns)
    mean_r = float(trade_returns.mean())
    std_r = float(trade_returns.std(ddof=1)) if n > 1 else 0.0

    # Trades per year — derive from average days between trades, fall back to
    # 1 trade/week (~50/yr) if we can't estimate.
    if n >= 2:
        first_d = outcomes[0].signal.fill_date
        last_d = outcomes[-1].exit_date
        total_days = max(1, (last_d - first_d).days)
        trades_per_year = n / (total_days / 365.0) if total_days > 0 else 50.0
    else:
        trades_per_year = 50.0

    # Sharpe (annualised): mean / std × sqrt(trades_per_year).
    sharpe = (mean_r / std_r) * math.sqrt(trades_per_year) if std_r > 0 else 0.0

    # Sortino: downside deviation only.
    negative_returns = trade_returns[trade_returns < 0]
    downside_std = float(negative_returns.std(ddof=1)) if len(negative_returns) > 1 else 0.0
    sortino = (mean_r / downside_std) * math.sqrt(trades_per_year) if downside_std > 0 else 0.0

    # Equity curve + drawdown.
    equity = _equity_curve(outcomes, risk_per_trade)
    cumulative_return = float(equity[-1] - 1.0)
    peaks = np.maximum.accumulate(equity)
    drawdowns = (equity - peaks) / peaks
    max_drawdown = float(drawdowns.min())  # negative

    # CAGR.
    years = max(0.01, (outcomes[-1].exit_date - outcomes[0].signal.fill_date).days / 365.0)
    cagr = (equity[-1]) ** (1 / years) - 1.0 if equity[-1] > 0 else -1.0

    calmar = cagr / abs(max_drawdown) if max_drawdown < 0 else 0.0

    return ReturnStats(
        sharpe_annualised=sharpe,
        sortino_annualised=sortino,
        calmar=calmar,
        max_drawdown_pct=max_drawdown * 100.0,
        cumulative_return_pct=cumulative_return * 100.0,
        cagr_pct=cagr * 100.0,
    )


def evaluate(
    outcomes: Iterable[TradeOutcome],
    risk_per_trade: float = 0.01,
) -> BacktestReport:
    """Compute the full report from a list of :class:`TradeOutcome`.

    Args:
        outcomes: trade outcomes in time order.
        risk_per_trade: account-risk fraction per trade for equity-curve
            construction. Default 0.01 = 1% (lower end of swing-position-sizing).

    Returns:
        :class:`BacktestReport` including the doctrine's deployment gate
        check (Sharpe > 1.0 AND max_drawdown < 25%).
    """
    outcomes_list = list(outcomes)
    trades = _compute_trade_stats(outcomes_list)
    risk = _compute_risk_stats(outcomes_list)
    returns = _compute_return_stats(outcomes_list, risk_per_trade=risk_per_trade)

    by_exit_reason: dict[str, int] = {}
    for o in outcomes_list:
        by_exit_reason[o.exit_reason] = by_exit_reason.get(o.exit_reason, 0) + 1

    by_grade: dict[str, list[TradeOutcome]] = {}
    for o in outcomes_list:
        by_grade.setdefault(o.signal.setup_grade, []).append(o)
    by_setup_grade = {g: _compute_trade_stats(os) for g, os in by_grade.items()}

    gate_passed = (
        returns.sharpe_annualised > 1.0
        and abs(returns.max_drawdown_pct) < 25.0
        and trades.n_trades >= 30  # minimum sample size to call the gate meaningful
    )

    note = ""
    if trades.n_trades < 30:
        note = (
            f"sample size {trades.n_trades} < 30; gate is preliminary — "
            "extend the universe or window before drawing deployment conclusions"
        )

    return BacktestReport(
        trades=trades,
        risk=risk,
        returns=returns,
        deployment_gate_passed=gate_passed,
        by_exit_reason=by_exit_reason,
        by_setup_grade=by_setup_grade,
        note=note,
    )
