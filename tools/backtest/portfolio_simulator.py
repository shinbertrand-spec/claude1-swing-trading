"""Portfolio-equity simulator — net-of-cost, dollar-level, cap-weighted.

Phase 1c/1d of the gate-hardening (2026-06-17). The legacy per-trade simulator
(simulator.py) is zero-cost, equal-RISK, infinite-capital, and fills EVERY
signal at the next open. This module replaces those assumptions for the
deployment gate with a realistic dollar portfolio:

  * Dollar positions + cash + concurrent-position cap (real capital constraint).
  * Cap-weight via dollar-ADV tilt (see SIZING NOTE) — never equal-weight.
  * OHLC-based fills (see FILL NOTE) — winners that gap past the limit are
    MISSED, not filled at the open.
  * Net-of-cost: half effective spread + sqrt-law impact charged on every fill
    (tools.backtest.cost_model).
  * Daily mark-to-market equity curve → NET Sharpe / max-drawdown.

FILL NOTE (OHLC-based, knob-free):
  momentum kinds → marketable limit = pivot×1.03 (entry_pricing). Fills at the
    bar OPEN iff open <= limit (gap <= 3%); a larger gap — often the biggest
    winner — is MISSED.
  reversion kinds → resting limit = pivot. Fills iff the bar LOW <= pivot (price
    traded down to it); a name that never dips is MISSED.
  The missed-winner selection emerges mechanically from OHLC — no 65% knob.
  `PortfolioResult` reports the realized fill rate + filled-vs-missed forward
  returns so the selection bias is measurable.

SIZING NOTE (dollar-ADV tilt as the cap-weight proxy):
  True point-in-time market-cap weighting needs a historical shares-outstanding
  panel we don't have. dollar-ADV is collinear with cap, point-in-time clean,
  and directly expresses "large liquid names dominate, thin names barely count"
  — which is the survivability thesis. Each position targets
  max_pct_per_position × clamp(ADV / REF_ADV, floor, 1.0). A follow-up can swap
  in a real cap panel without changing the interface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from ..auto_paper import entry_pricing
from . import cost_model, security_master
from .setup_replay import TradeSignal


@dataclass
class PortfolioConfig:
    starting_equity: float = 1_000_000.0
    max_positions: int = 8                 # CLAUDE.md concurrent cap
    max_pct_per_position: float = 0.05     # CLAUDE.md 5% per position
    ref_adv_full_weight: float = 20_000_000.0  # ADV at/above which a name gets full weight
    min_liquidity_factor: float = 0.2      # floor on the ADV tilt (never size to ~0)
    apply_costs: bool = True
    adv_window: int = security_master.ADV_WINDOW_DEFAULT


@dataclass
class NetTradeOutcome:
    ticker: str
    setup_type: str
    kind: str
    fill_date: date
    exit_date: date
    entry_fill_price: float       # gross fill (pre-cost)
    entry_net_price: float        # post-cost (what we paid)
    exit_fill_price: float
    exit_net_price: float
    shares: int
    exit_reason: str
    bars_held: int
    net_return: float             # net P&L / net cost basis
    gross_return: float           # gross (pre-cost) for diagnostics


@dataclass
class PortfolioResult:
    equity_curve: pd.Series                # daily MTM equity, indexed by date
    trades: list[NetTradeOutcome]
    n_signals: int
    n_filled: int
    n_missed: int
    fill_rate: float
    avg_filled_fwd_return: float           # mean gross fwd return of FILLED entries
    avg_missed_fwd_return: float           # mean gross fwd return of MISSED entries (winner-selection check)
    sharpe_annualised: float
    max_drawdown_pct: float
    n_trades: int
    deployment_gate_passed: bool
    notes: list[str] = field(default_factory=list)


def _to_date_index(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df.index, pd.DatetimeIndex):
        df = df.copy()
        df.index = pd.to_datetime(df.index)
    return df


def _row_on_or_after(df: pd.DataFrame, d: date):
    """First bar with index date >= d (the fill bar). Returns (Timestamp, row) or None."""
    pos = df.index.searchsorted(pd.Timestamp(d))
    if pos >= len(df.index):
        return None
    return df.index[pos], df.iloc[pos]


def _close_on_or_before(df: pd.DataFrame, d: date) -> Optional[float]:
    pos = df.index.searchsorted(pd.Timestamp(d), side="right") - 1
    if pos < 0:
        return None
    return float(df.iloc[pos]["Close"])


def _compute_fill(kind: str, pivot: float, fill_bar) -> Optional[float]:
    """OHLC-based fill price for a buy, or None if the bar didn't reach the limit."""
    limit = entry_pricing.entry_limit_price(kind, pivot)
    open_p = float(fill_bar["Open"])
    low_p = float(fill_bar["Low"])
    if kind in entry_pricing.MOMENTUM_KINDS:
        # marketable limit: fills at the open iff the open is at/below the limit
        return open_p if open_p <= limit else None
    # reversion / default: resting limit at pivot; fills iff the bar traded down to it
    if low_p <= limit:
        return min(open_p, limit)
    return None


@dataclass
class _OpenPosition:
    sig: TradeSignal
    kind: str
    shares: int
    entry_fill_price: float
    entry_net_price: float
    fill_ts: pd.Timestamp
    fwd_ref_close: float          # signal-day close, for fwd-return diagnostics


def simulate(
    signals: list[TradeSignal],
    universe_dfs: dict[str, pd.DataFrame],
    config: PortfolioConfig | None = None,
    *,
    sharpe_min: float = 1.0,
    max_dd_pct: float = 25.0,
    n_min: int = 30,
) -> PortfolioResult:
    cfg = config or PortfolioConfig()
    dfs = {t: _to_date_index(df) for t, df in universe_dfs.items()}

    # Build the master calendar (union of all bars in the signal span + holds).
    all_idx = sorted({ts for df in dfs.values() for ts in df.index})
    if not all_idx:
        raise ValueError("no price bars in universe_dfs")

    # Index signals by their fill bar timestamp (first bar on/after fill_date).
    pending: dict[pd.Timestamp, list[tuple[TradeSignal, str, float]]] = {}
    n_signals = 0
    for sig in signals:
        df = dfs.get(sig.ticker)
        if df is None:
            continue
        pivot = _close_on_or_before(df, sig.entry_date)
        if pivot is None or pivot <= 0:
            continue
        hit = _row_on_or_after(df, sig.fill_date)
        if hit is None:
            continue
        fill_ts, _ = hit
        kind = entry_pricing.resolve_kind(sig.setup_type)
        pending.setdefault(fill_ts, []).append((sig, kind, pivot))
        n_signals += 1

    cash = cfg.starting_equity
    open_positions: list[_OpenPosition] = []
    trades: list[NetTradeOutcome] = []
    equity_points: list[tuple[pd.Timestamp, float]] = []
    n_filled = 0
    filled_fwd: list[float] = []
    missed_fwd: list[float] = []

    for ts in all_idx:
        d = ts.date()

        # 1. Exits on open positions (check this bar's OHLC).
        still_open: list[_OpenPosition] = []
        for pos in open_positions:
            df = dfs[pos.sig.ticker]
            if ts not in df.index:
                still_open.append(pos)
                continue
            bar = df.loc[ts]
            o, h, l, c = (float(bar["Open"]), float(bar["High"]),
                          float(bar["Low"]), float(bar["Close"]))
            bars_held = int(df.index.searchsorted(ts) - df.index.searchsorted(pos.fill_ts))
            stop = pos.sig.stop_price
            target = pos.sig.target_price
            exit_price: Optional[float] = None
            reason = ""
            if o <= stop:                       # gapped through the stop
                exit_price, reason = o, "gap_through_stop"
            elif l <= stop:
                exit_price, reason = stop, "stop_hit"
            elif target is not None and h >= target:
                exit_price, reason = target, "target_hit"
            elif bars_held >= pos.sig.max_hold_days:
                exit_price, reason = c, "max_hold"
            if exit_price is None:
                still_open.append(pos)
                continue
            # Apply sell cost.
            adv = security_master.dollar_adv(df, d, window=cfg.adv_window)
            half = security_master.liquidity_tier(adv).half_spread_bps
            gross_dollars = pos.shares * exit_price
            sell_cost_bps = cost_model.one_side_cost_bps(gross_dollars, adv, half) if cfg.apply_costs else 0.0
            net_exit = cost_model.apply_sell_cost(exit_price, sell_cost_bps)
            cash += pos.shares * net_exit
            net_return = (net_exit - pos.entry_net_price) / pos.entry_net_price
            gross_return = (exit_price - pos.entry_fill_price) / pos.entry_fill_price
            trades.append(NetTradeOutcome(
                ticker=pos.sig.ticker, setup_type=pos.sig.setup_type, kind=pos.kind,
                fill_date=pos.fill_ts.date(), exit_date=d,
                entry_fill_price=pos.entry_fill_price, entry_net_price=pos.entry_net_price,
                exit_fill_price=exit_price, exit_net_price=net_exit,
                shares=pos.shares, exit_reason=reason, bars_held=bars_held,
                net_return=net_return, gross_return=gross_return,
            ))
        open_positions = still_open

        # 2. New fills on signals whose fill bar is today.
        for sig, kind, pivot in pending.get(ts, []):
            df = dfs[sig.ticker]
            fill_bar = df.loc[ts]
            # Forward-return diagnostic (gross, ref = signal-day close → +max_hold close).
            fwd = _forward_return(df, ts, sig.max_hold_days, pivot)
            fill_price = _compute_fill(kind, pivot, fill_bar)
            if fill_price is None:
                if fwd is not None:
                    missed_fwd.append(fwd)
                continue
            if len(open_positions) >= cfg.max_positions:
                continue  # at concurrency cap — capital exhausted
            # Cap-weight via ADV tilt.
            adv = security_master.dollar_adv(df, d, window=cfg.adv_window)
            liq_factor = cfg.min_liquidity_factor
            if adv is not None and cfg.ref_adv_full_weight > 0:
                liq_factor = max(cfg.min_liquidity_factor,
                                 min(1.0, adv / cfg.ref_adv_full_weight))
            equity_now = cash + sum(
                p.shares * (_close_on_or_before(dfs[p.sig.ticker], d) or p.entry_fill_price)
                for p in open_positions
            )
            target_dollars = min(cfg.max_pct_per_position * liq_factor * equity_now, cash)
            half = security_master.liquidity_tier(adv).half_spread_bps
            buy_cost_bps = cost_model.one_side_cost_bps(target_dollars, adv, half) if cfg.apply_costs else 0.0
            net_buy = cost_model.apply_buy_cost(fill_price, buy_cost_bps)
            shares = int(target_dollars // net_buy)
            if shares <= 0:
                continue
            cash -= shares * net_buy
            open_positions.append(_OpenPosition(
                sig=sig, kind=kind, shares=shares,
                entry_fill_price=fill_price, entry_net_price=net_buy,
                fill_ts=ts, fwd_ref_close=pivot,
            ))
            n_filled += 1
            if fwd is not None:
                filled_fwd.append(fwd)

        # 3. Mark-to-market equity at this bar's close.
        mtm = cash + sum(
            p.shares * float(dfs[p.sig.ticker].loc[ts]["Close"])
            for p in open_positions if ts in dfs[p.sig.ticker].index
        )
        equity_points.append((ts, mtm))

    equity = pd.Series(
        [v for _, v in equity_points],
        index=pd.DatetimeIndex([t for t, _ in equity_points]),
    )
    sharpe, mdd = _equity_metrics(equity)
    n_trades = len(trades)
    n_missed = n_signals - n_filled
    gate = (sharpe > sharpe_min and abs(mdd) < max_dd_pct and n_trades >= n_min)

    return PortfolioResult(
        equity_curve=equity,
        trades=trades,
        n_signals=n_signals,
        n_filled=n_filled,
        n_missed=n_missed,
        fill_rate=(n_filled / n_signals) if n_signals else 0.0,
        avg_filled_fwd_return=float(np.mean(filled_fwd)) if filled_fwd else 0.0,
        avg_missed_fwd_return=float(np.mean(missed_fwd)) if missed_fwd else 0.0,
        sharpe_annualised=sharpe,
        max_drawdown_pct=mdd,
        n_trades=n_trades,
        deployment_gate_passed=gate,
    )


def _forward_return(df: pd.DataFrame, fill_ts: pd.Timestamp, hold_days: int, pivot: float) -> Optional[float]:
    """Gross forward return from the signal pivot to the close ~hold_days later
    (diagnostic for filled-vs-missed selection)."""
    start = df.index.searchsorted(fill_ts)
    end = min(start + hold_days, len(df.index) - 1)
    if end <= start or pivot <= 0:
        return None
    return float(df.iloc[end]["Close"]) / pivot - 1.0


def _equity_metrics(equity: pd.Series) -> tuple[float, float]:
    """Annualised Sharpe (daily returns × sqrt(252)) and max drawdown %."""
    if len(equity) < 3:
        return 0.0, 0.0
    rets = equity.pct_change().dropna()
    if rets.std(ddof=1) == 0 or len(rets) < 2:
        sharpe = 0.0
    else:
        sharpe = float(rets.mean() / rets.std(ddof=1) * np.sqrt(252))
    peaks = equity.cummax()
    dd = (equity - peaks) / peaks
    mdd = float(dd.min() * 100.0)
    return sharpe, mdd
