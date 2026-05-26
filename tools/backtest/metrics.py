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

**2026-05-26 — concurrent-position cap added** to address the simulator
concurrency bug surfaced by the dual_ma_trend_following run on sp500_2026q2
(Sharpe 3.02 + DD -70.62% — internally inconsistent on the 503-ticker
universe). The :func:`_apply_concurrent_cap` helper filters outcomes by
``fill_date`` order, dropping any trade that would have exceeded the
``max_concurrent`` cap given still-open positions at its entry. The
default is 8 — the CLAUDE.md "Maximum 8 concurrent open positions" hard
rule. Pass ``max_concurrent=None`` to disable (for parity with the
pre-fix behavior, e.g. when a strategy spec has its own per-strategy
concurrency control like xs_short_term_reversal's ``bottom_pct`` or
connors_rsi2's ``max_concurrent_positions``).
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


@dataclass
class AggregateGateResult:
    """Composite deployment gate for rolling-walk-forward backtests.

    The doctrine deployment gate has two clauses:

    1. **Aggregate** — the concatenated-OOS report meets the doctrine
       thresholds (Sharpe > sharpe_min, |MDD| < max_dd_pct, n >= n_min).
    2. **Per-window** — at least ``min_window_pass_rate`` of the
       per-window OOS reports individually clear Sharpe > min_window_sharpe.

    Both must pass. The per-window clause catches the case where one
    outlier window carries an otherwise-fragile aggregate (e.g. SEPA-VCP
    2023/2024/2025 OOS Sharpe = -0.44 / 4.27 / -1.38 — aggregate Sharpe
    1.37 PASSES the original gate, but 2 of 3 windows individually have
    negative Sharpe, indicating real regime fragility hidden by one
    outlier year).
    """
    # Aggregate clause
    aggregate_sharpe: float
    aggregate_max_dd_pct: float
    aggregate_n_trades: int
    aggregate_gate_passed: bool

    # Per-window clause
    n_windows: int
    n_windows_above_floor: int
    window_pass_rate: float
    window_clause_passed: bool

    # Thresholds (for audit + reproducibility)
    sharpe_min: float
    max_dd_pct: float
    n_min: int
    min_window_sharpe: float
    min_window_pass_rate: float

    # Final composite verdict
    gate_passed: bool
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

    Trades are compounded in ``exit_date`` order so the cumulative-return
    series reflects portfolio P&L realized through time. Apply
    :func:`_apply_concurrent_cap` upstream if you want to honor a
    max-concurrent-position limit.
    """
    ordered = sorted(outcomes, key=lambda o: o.exit_date)
    trade_returns = np.array([risk_per_trade * o.r_multiple for o in ordered])
    # Geometric compounding.
    return np.cumprod(1 + trade_returns)


def _apply_concurrent_cap(
    outcomes: list[TradeOutcome],
    max_concurrent: int,
) -> list[TradeOutcome]:
    """Drop trades that would have exceeded ``max_concurrent`` open positions.

    Walks outcomes in ``fill_date`` order. At each candidate's fill_date,
    counts still-open positions (those whose ``exit_date > fill_date``).
    If the open count is already at ``max_concurrent``, the candidate is
    rejected — i.e. on a real account with the framework's hard rule of
    8 concurrent positions, the strategy could not have placed the trade.

    Same-day handoff is permissive: a trade exiting on day D leaves room
    for a new trade filling on day D. Rationale: real cash from the exit
    is available before the new entry order is placed; treating the slot
    as freed by EOD matches typical T+0 settlement on liquid US equities.

    Tie-breaking when fill_dates collide: ties on ``fill_date`` are
    resolved by ``(ticker, exit_date)`` lexically. Stable for reproducibility.
    """
    if max_concurrent is None or max_concurrent <= 0 or not outcomes:
        return list(outcomes)

    sorted_by_fill = sorted(
        outcomes,
        key=lambda o: (o.signal.fill_date, o.signal.ticker, o.exit_date),
    )
    accepted: list[TradeOutcome] = []
    open_exits: list = []  # exit dates of currently-open accepted positions
    for o in sorted_by_fill:
        # Drop positions that have closed on or before this fill_date.
        open_exits = [d for d in open_exits if d > o.signal.fill_date]
        if len(open_exits) >= max_concurrent:
            continue
        accepted.append(o)
        open_exits.append(o.exit_date)
    return accepted


def _compute_return_stats(
    outcomes: list[TradeOutcome],
    risk_per_trade: float = 0.01,
) -> ReturnStats:
    if not outcomes:
        return ReturnStats(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    # Mean / std / Sharpe / Sortino are order-invariant; computed on the
    # list as-given. Equity-curve-derived stats (cumulative return, DD,
    # CAGR) are time-ordered via _equity_curve's exit-date sort.
    trade_returns = np.array([risk_per_trade * o.r_multiple for o in outcomes])
    n = len(trade_returns)
    mean_r = float(trade_returns.mean())
    std_r = float(trade_returns.std(ddof=1)) if n > 1 else 0.0

    # Trades per year — derive from min(fill_date) → max(exit_date) span,
    # fall back to 1 trade/week (~50/yr) if we can't estimate.
    if n >= 2:
        first_d = min(o.signal.fill_date for o in outcomes)
        last_d = max(o.exit_date for o in outcomes)
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

    # Equity curve + drawdown (exit-date-ordered inside _equity_curve).
    equity = _equity_curve(outcomes, risk_per_trade)
    cumulative_return = float(equity[-1] - 1.0)
    peaks = np.maximum.accumulate(equity)
    drawdowns = (equity - peaks) / peaks
    max_drawdown = float(drawdowns.min())  # negative

    # CAGR over the realised span.
    first_d = min(o.signal.fill_date for o in outcomes)
    last_d = max(o.exit_date for o in outcomes)
    years = max(0.01, (last_d - first_d).days / 365.0)
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


DEFAULT_MAX_CONCURRENT = 8


def evaluate(
    outcomes: Iterable[TradeOutcome],
    risk_per_trade: float = 0.01,
    max_concurrent: int | None = DEFAULT_MAX_CONCURRENT,
) -> BacktestReport:
    """Compute the full report from a list of :class:`TradeOutcome`.

    Args:
        outcomes: trade outcomes in time order.
        risk_per_trade: account-risk fraction per trade for equity-curve
            construction. Default 0.01 = 1% (lower end of swing-position-sizing).
        max_concurrent: maximum simultaneously-open positions allowed when
            building the equity curve. Default 8 — the CLAUDE.md hard rule.
            Any trade whose fill would have pushed the open count above
            this cap is dropped from ALL downstream stats (trade count,
            Sharpe sample, exit-reason histogram, per-grade breakdown).
            Pass ``None`` to disable (recovers pre-2026-05-26 behavior).

    Returns:
        :class:`BacktestReport` including the doctrine's deployment gate
        check (Sharpe > 1.0 AND max_drawdown < 25%). ``note`` records the
        number of cap-rejected outcomes if ``max_concurrent`` filtered any.
    """
    outcomes_list = list(outcomes)
    n_before_cap = len(outcomes_list)
    if max_concurrent is not None:
        outcomes_list = _apply_concurrent_cap(outcomes_list, max_concurrent)
    n_rejected = n_before_cap - len(outcomes_list)

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

    note_parts: list[str] = []
    if n_rejected > 0:
        note_parts.append(
            f"concurrent-position cap (max_concurrent={max_concurrent}) "
            f"rejected {n_rejected}/{n_before_cap} outcomes; stats reflect "
            f"the {len(outcomes_list)} that would have been on book"
        )
    if trades.n_trades < 30:
        note_parts.append(
            f"sample size {trades.n_trades} < 30; gate is preliminary — "
            "extend the universe or window before drawing deployment conclusions"
        )
    note = " | ".join(note_parts)

    return BacktestReport(
        trades=trades,
        risk=risk,
        returns=returns,
        deployment_gate_passed=gate_passed,
        by_exit_reason=by_exit_reason,
        by_setup_grade=by_setup_grade,
        note=note,
    )


def evaluate_aggregated_with_windows(
    aggregate_report: BacktestReport,
    window_reports: list[BacktestReport],
    *,
    sharpe_min: float = 1.0,
    max_dd_pct: float = 25.0,
    n_min: int = 30,
    min_window_sharpe: float = 0.5,
    min_window_pass_rate: float = 0.5,
) -> AggregateGateResult:
    """Compose the tightened doctrine deployment gate.

    Args:
        aggregate_report: Result of :func:`evaluate` on the concatenated OOS
            outcomes across all rolling walk-forward windows.
        window_reports: Per-window OOS reports (each from :func:`evaluate`),
            in window order. The aggregate-only gate (the original doctrine
            check) is preserved as the first clause; the per-window clause
            is the new check.
        sharpe_min: Aggregate-clause Sharpe threshold. Default 1.0 — doctrine.
        max_dd_pct: Aggregate-clause max-drawdown threshold (absolute).
            Default 25.0 — doctrine.
        n_min: Aggregate-clause minimum sample size. Default 30 — doctrine.
        min_window_sharpe: Per-window Sharpe floor. A window passes if its
            OOS Sharpe strictly exceeds this. Default 0.5 — chosen so that a
            window with Sharpe in (0, 0.5] is treated as non-trivially weak.
        min_window_pass_rate: Minimum fraction of windows that must
            individually clear ``min_window_sharpe``. Default 0.5 — majority.

    Returns:
        :class:`AggregateGateResult` — both clause verdicts plus the
        composite ``gate_passed`` (AND of both).

    The new clause turns "5 of 5 deployable" into "5 of 5 deployable AND
    individually robust across regimes". A setup whose aggregate Sharpe is
    carried by one outlier window fails this gate.
    """
    agg = aggregate_report
    aggregate_passed = (
        agg.returns.sharpe_annualised > sharpe_min
        and abs(agg.returns.max_drawdown_pct) < max_dd_pct
        and agg.trades.n_trades >= n_min
    )

    n_windows = len(window_reports)
    n_above_floor = sum(
        1 for w in window_reports
        if w.returns.sharpe_annualised > min_window_sharpe
    )
    pass_rate = n_above_floor / n_windows if n_windows > 0 else 0.0
    window_clause_passed = n_windows > 0 and pass_rate >= min_window_pass_rate

    overall = aggregate_passed and window_clause_passed

    notes: list[str] = []
    if not aggregate_passed:
        reasons: list[str] = []
        if agg.returns.sharpe_annualised <= sharpe_min:
            reasons.append(
                f"Sharpe {agg.returns.sharpe_annualised:.2f} <= {sharpe_min}"
            )
        if abs(agg.returns.max_drawdown_pct) >= max_dd_pct:
            reasons.append(
                f"|MDD| {abs(agg.returns.max_drawdown_pct):.2f}% >= {max_dd_pct}%"
            )
        if agg.trades.n_trades < n_min:
            reasons.append(f"n {agg.trades.n_trades} < {n_min}")
        notes.append("aggregate fails: " + "; ".join(reasons))
    if n_windows == 0:
        notes.append(
            "no per-window OOS reports supplied — window clause cannot be evaluated"
        )
    elif not window_clause_passed:
        notes.append(
            f"only {n_above_floor}/{n_windows} windows have Sharpe > "
            f"{min_window_sharpe} (pass rate {pass_rate:.2f} < "
            f"{min_window_pass_rate})"
        )

    return AggregateGateResult(
        aggregate_sharpe=agg.returns.sharpe_annualised,
        aggregate_max_dd_pct=agg.returns.max_drawdown_pct,
        aggregate_n_trades=agg.trades.n_trades,
        aggregate_gate_passed=aggregate_passed,
        n_windows=n_windows,
        n_windows_above_floor=n_above_floor,
        window_pass_rate=pass_rate,
        window_clause_passed=window_clause_passed,
        sharpe_min=sharpe_min,
        max_dd_pct=max_dd_pct,
        n_min=n_min,
        min_window_sharpe=min_window_sharpe,
        min_window_pass_rate=min_window_pass_rate,
        gate_passed=overall,
        note=" | ".join(notes),
    )
