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
  momentum kinds → marketable limit = pivot×(1+buffer) (entry_pricing). Fills at
    the bar OPEN iff open <= limit (gap <= buffer); a larger gap — often the
    biggest winner — is MISSED.
  reversion kinds → resting limit = pivot. Fills iff the bar LOW <= pivot (price
    traded down to it); a name that never dips is MISSED.
  The missed-winner selection emerges mechanically from OHLC — no 65% knob.
  `PortfolioResult` reports the realized fill rate + filled-vs-missed forward
  returns so the selection bias is measurable.

FILL MODELS (``fill_model`` arg — momentum class only; reversion is always a
passive limit):
  "marketable_limit" (DEFAULT, LIVE-ALIGNED): momentum fills at the open iff the
    gap from the prior close is <= the buffer (``momentum_buffer``, default =
    entry_pricing.MOMENTUM_ENTRY_BUFFER = 3%). This is the executable live model
    (you know the prior close, so you can place ``prior_close×(1+buffer)`` as a
    DAY marketable limit). A pathological gap is skipped, NOT chased.
  "pure_moo" (DIAGNOSTIC UPPER BOUND): momentum ALWAYS fills at the next bar's
    open, no gap cap — the "get exposure ≫ shave entry price" model (Clenow;
    AQR "have your momentum and eat it too"). Not directly live-executable under
    CLAUDE.md (it needs a market-on-open order), but it reveals how much alpha
    the buffer cap truncates by missing the biggest overnight gappers (the exact
    subset where momentum's overnight return — Lou-Polk-Skouras 2019 — fires).
  Sweeping ``momentum_buffer`` from small (0.5%) to large (3%) up to pure_moo
  makes the gap-risk-vs-miss tradeoff visible in the certification numbers.

SIZING NOTE (dollar-ADV tilt as the cap-weight proxy):
  True point-in-time market-cap weighting needs a historical shares-outstanding
  panel we don't have. dollar-ADV is collinear with cap, point-in-time clean,
  and directly expresses "large liquid names dominate, thin names barely count"
  — which is the survivability thesis. Each position targets
  max_pct_per_position × clamp(ADV / REF_ADV, floor, 1.0). A follow-up can swap
  in a real cap panel without changing the interface.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from ..auto_paper import entry_pricing
from . import cost_model, security_master
from .setup_replay import TradeSignal
from .walk_forward import WindowSpec, rolling_splits

# Exit reasons that CROSS the book (market orders) and so pay the full spread,
# vs a passive limit target-sell which earns/halves it.
_MARKETABLE_EXITS = frozenset({"gap_through_stop", "stop_hit", "max_hold"})


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
    fill_model: str = "marketable_limit"
    momentum_buffer: Optional[float] = None


def _to_date_index(df: pd.DataFrame) -> pd.DataFrame:
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        idx = pd.to_datetime(idx)
    if idx.tz is not None:        # normalise tz-aware (yfinance) → naive for comparisons
        idx = idx.tz_localize(None)
    if idx is not df.index:
        df = df.copy()
        df.index = idx
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


# Fill-model identifiers.
FILL_MARKETABLE_LIMIT = "marketable_limit"   # live-aligned (default)
FILL_PURE_MOO = "pure_moo"                   # diagnostic upper bound
_VALID_FILL_MODELS = (FILL_MARKETABLE_LIMIT, FILL_PURE_MOO)


def _momentum_limit(kind: str, pivot: float, momentum_buffer: Optional[float]) -> float:
    """Marketable-limit price for a momentum buy. ``momentum_buffer`` overrides
    the live ``entry_pricing.MOMENTUM_ENTRY_BUFFER`` for the diagnostic buffer
    sweep; ``None`` uses the live-authoritative value (keeps cert == live)."""
    if momentum_buffer is None:
        return entry_pricing.entry_limit_price(kind, pivot)
    return round(pivot * (1.0 + float(momentum_buffer)), 2)


def _compute_fill(
    kind: str,
    pivot: float,
    fill_bar,
    *,
    fill_model: str = FILL_MARKETABLE_LIMIT,
    momentum_buffer: Optional[float] = None,
) -> Optional[float]:
    """OHLC-based fill price for a buy, or None if the bar didn't reach the limit.

    Momentum class honours ``fill_model``:
      * ``pure_moo`` → always fill at the bar OPEN (no gap cap).
      * ``marketable_limit`` → fill at the open iff open <= pivot×(1+buffer).
    Reversion class is always a passive resting limit at the pivot regardless of
    ``fill_model`` (a reversion buy is adverse-selected by chasing up).
    """
    open_p = float(fill_bar["Open"])
    low_p = float(fill_bar["Low"])
    if kind in entry_pricing.MOMENTUM_KINDS:
        if fill_model == FILL_PURE_MOO:
            return open_p                      # market-on-open: always fills
        limit = _momentum_limit(kind, pivot, momentum_buffer)
        # marketable limit: fills at the open iff the open is at/below the limit
        return open_p if open_p <= limit else None
    # reversion / default: resting limit at pivot; fills iff the bar traded down to it
    limit = entry_pricing.entry_limit_price(kind, pivot)
    if low_p <= limit:
        return min(open_p, limit)
    return None


def _fill_kept(ticker: str, ts: pd.Timestamp, fill_probability: float) -> bool:
    """Deterministic fill-realization gate: keep this marketable entry with
    probability ``fill_probability``, decided reproducibly from (ticker, date)
    so a re-run is identical (no RNG state). Models partial fills / auction
    misses on a live DAY marketable limit."""
    if fill_probability >= 1.0:
        return True
    if fill_probability <= 0.0:
        return False
    key = f"{ticker}|{pd.Timestamp(ts).date().isoformat()}".encode("utf-8")
    draw = int(hashlib.md5(key).hexdigest()[:8], 16) / 0xFFFFFFFF
    return draw < fill_probability


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
    fill_model: str = FILL_MARKETABLE_LIMIT,
    momentum_buffer: Optional[float] = None,
    full_spread_marketable: bool = True,
    entry_slippage_bps: float = 0.0,
    fill_probability: float = 1.0,
) -> PortfolioResult:
    """Simulate the net-of-cost portfolio.

    Cost realism knobs (added 2026-06-20 after adversarial review):
      full_spread_marketable: charge the FULL effective spread on marketable
        crossings — momentum buys + market-type exits (stop / gap / max-hold).
        A passive reversion buy and a limit target-sell stay at half-spread.
        Default True (the corrected, honest model).
      entry_slippage_bps: extra adverse bps paid above the modeled open fill on
        a marketable momentum entry — models the auction printing above the
        open / the limit ticking. Default 0 (no stress).
      fill_probability: deterministic fraction of OTHERWISE-fillable marketable
        momentum entries that actually fill — models partial fills / auction
        misses on a DAY marketable limit. Default 1.0 (all fill). A thinned
        fill is recorded as a MISS (not a win). Deterministic per (ticker,date)
        so re-runs are reproducible.
    """
    if fill_model not in _VALID_FILL_MODELS:
        raise ValueError(
            f"fill_model must be one of {_VALID_FILL_MODELS}, got {fill_model!r}"
        )
    if not 0.0 <= fill_probability <= 1.0:
        raise ValueError(f"fill_probability must be in [0,1], got {fill_probability}")
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
            # Apply sell cost. Market-type exits (stop/gap/max-hold) CROSS the
            # book → full spread; a passive target limit-sell stays at half.
            adv = security_master.dollar_adv(df, d, window=cfg.adv_window)
            half = security_master.liquidity_tier(adv).half_spread_bps
            gross_dollars = pos.shares * exit_price
            exit_crosses = full_spread_marketable and reason in _MARKETABLE_EXITS
            sell_cost_bps = (
                cost_model.one_side_cost_bps(gross_dollars, adv, half, marketable_cross=exit_crosses)
                if cfg.apply_costs else 0.0
            )
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
            is_marketable_buy = kind in entry_pricing.MOMENTUM_KINDS
            fill_price = _compute_fill(
                kind, pivot, fill_bar,
                fill_model=fill_model, momentum_buffer=momentum_buffer,
            )
            # Deterministic fill-realization haircut (marketable entries only):
            # a thinned fill counts as a MISS, not a win.
            if (fill_price is not None and is_marketable_buy
                    and not _fill_kept(sig.ticker, ts, fill_probability)):
                fill_price = None
            if fill_price is None:
                if fwd is not None:
                    missed_fwd.append(fwd)
                continue
            if len(open_positions) >= cfg.max_positions:
                continue  # at concurrency cap — capital exhausted
            # Adverse-fill slippage above the modeled open (marketable entries).
            if is_marketable_buy and entry_slippage_bps:
                fill_price = fill_price * (1.0 + entry_slippage_bps / 10_000.0)
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
            # Momentum buys place a MARKETABLE limit → cross the full spread.
            buy_cost_bps = (
                cost_model.one_side_cost_bps(
                    target_dollars, adv, half,
                    marketable_cross=(is_marketable_buy and full_spread_marketable),
                ) if cfg.apply_costs else 0.0
            )
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
        fill_model=fill_model,
        momentum_buffer=momentum_buffer,
    )


@dataclass
class WalkForwardResult:
    """Rolling walk-forward verdict under the net-of-cost portfolio simulator.

    The robustness clause the single 70/30 split cannot give: the aggregate gate
    can be carried by one strong window, so we ALSO require a majority of the
    individual OOS windows to clear a per-window Sharpe floor — mirroring
    ``metrics.evaluate_aggregated_with_windows`` but under the realistic fill +
    cost model (the legacy per-window 6/6 was computed zero-cost and does NOT
    transfer).
    """
    window_results: list[tuple[WindowSpec, PortfolioResult]]
    aggregate: PortfolioResult
    n_windows: int
    n_windows_above_floor: int
    window_pass_rate: float
    window_clause_passed: bool
    aggregate_gate_passed: bool
    overall_passed: bool
    min_window_sharpe: float
    min_window_pass_rate: float


def simulate_walk_forward(
    signals: list[TradeSignal],
    universe_dfs: dict[str, pd.DataFrame],
    *,
    start: date,
    end: date,
    is_years: int = 3,
    oos_years: int = 1,
    step_years: int = 1,
    sharpe_min: float = 1.0,
    max_dd_pct: float = 25.0,
    n_min: int = 30,
    min_window_sharpe: float = 0.5,
    min_window_pass_rate: float = 0.5,
    config: PortfolioConfig | None = None,
    **sim_kwargs,
) -> WalkForwardResult:
    """Run the net-of-cost simulator over each rolling OOS window AND the union.

    Each OOS window is simulated as its own fresh-capital portfolio → its own
    Sharpe. The per-window clause requires ``>= min_window_pass_rate`` of windows
    to individually clear ``min_window_sharpe``. The aggregate clause runs the
    union of all OOS-window signals through the gate (sharpe_min ∧ max_dd ∧ n).
    Both must pass. ``sim_kwargs`` (fill_model / momentum_buffer /
    full_spread_marketable / entry_slippage_bps / fill_probability) flow into
    EVERY window run so the cost/fill model is identical across windows.
    """
    specs = rolling_splits(start, end, is_years, oos_years, step_years)
    window_results: list[tuple[WindowSpec, PortfolioResult]] = []
    all_oos: list[TradeSignal] = []
    for spec in specs:
        oos = [s for s in signals
               if spec.in_sample_end <= s.fill_date < spec.out_of_sample_end]
        all_oos.extend(oos)
        res = simulate(
            oos, universe_dfs, config,
            sharpe_min=sharpe_min, max_dd_pct=max_dd_pct, n_min=n_min,
            **sim_kwargs,
        )
        window_results.append((spec, res))

    aggregate = simulate(
        all_oos, universe_dfs, config,
        sharpe_min=sharpe_min, max_dd_pct=max_dd_pct, n_min=n_min,
        **sim_kwargs,
    )
    n_windows = len(specs)
    n_above = sum(1 for _, r in window_results
                  if r.sharpe_annualised > min_window_sharpe)
    pass_rate = (n_above / n_windows) if n_windows else 0.0
    window_clause = n_windows > 0 and pass_rate >= min_window_pass_rate
    agg_gate = aggregate.deployment_gate_passed
    return WalkForwardResult(
        window_results=window_results,
        aggregate=aggregate,
        n_windows=n_windows,
        n_windows_above_floor=n_above,
        window_pass_rate=pass_rate,
        window_clause_passed=window_clause,
        aggregate_gate_passed=agg_gate,
        overall_passed=bool(window_clause and agg_gate),
        min_window_sharpe=min_window_sharpe,
        min_window_pass_rate=min_window_pass_rate,
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
