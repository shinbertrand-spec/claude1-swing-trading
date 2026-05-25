"""Paper-auto performance dashboard — realized vs backtest comparison.

Per CLAUDE.md § Paper-auto carve-out → Scope progression (Session 4): once
the paper-auto track has accumulated closed trades, this module answers
"**are the deployable strategies actually working live, or did backtest
overstate the edge?**"

Operates exclusively on the paper-auto track (``ledgers/paper-auto/`` +
``journal/paper-auto/positions.json``). Reuses the backtest's metrics
module (:mod:`tools.backtest.metrics`) for trade + return statistics —
SAME vocabulary as the deployment gate (``Sharpe > 1.0 AND |max DD| <
25% AND n >= 30``), so the comparison is apples-to-apples.

Equity-curve simplification (vs the backtest's bar-by-bar tracking):
this module converts each closed trade to an R-multiple and then to a
synthetic dollar return at ``risk_per_trade`` (default 1%) using
:func:`tools.backtest.metrics._equity_curve`. This means the Sharpe /
max-DD numbers here are TRADE-SEQUENCE-DRIVEN, not bar-driven — the
intra-trade drawdown isn't captured. For 30+ trades that's usually
within ~10% of the bar-driven number; for thin samples it can be
noisier. Documented in the dashboard's "Notes" section.

Public API:

* :func:`compute_performance` — scan closed paper-auto ledgers, build
  realized trade list, compute TradeStats + ReturnStats + per-setup
  breakdown + vs-backtest comparison.
* :func:`compute_open_pnl` — pull current unrealized P&L on open
  paper-auto positions from Tiger.

Session 3 dependency: the close-out write path that sets
``position_state.exit_price`` lives in Session 3. If Session 3 hasn't
merged yet, closed-position counts will be zero (positions sit forever
in ``starter``) and realized stats will be empty. The dashboard handles
this gracefully — empty report, no crash.
"""
from __future__ import annotations

import datetime as _dt
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import yaml

from ..backtest.metrics import (
    ReturnStats,
    TradeStats,
    _compute_return_stats,
    _compute_trade_stats,
)
from ..backtest.setup_replay import TradeSignal
from ..backtest.simulator import TradeOutcome
from . import config, state

# Default risk fraction used to convert R-multiples to a synthetic equity
# curve. Matches the backtest module's default (lower-end of swing
# position-sizing's risk budget).
DEFAULT_RISK_PER_TRADE = 0.01

# Tolerance bands for the realized-vs-backtest comparison status flag.
# A setup is "ok" when realized Sharpe is within 25% of backtest;
# "warn" within 50% OR sample size < 30; "fail" below 50% AND n >= 30.
SHARPE_TOLERANCE_OK = 0.75       # realized >= 0.75 × backtest
SHARPE_TOLERANCE_WARN = 0.50     # realized >= 0.50 × backtest (warn band)
MIN_TRADES_FOR_VERDICT = 30      # below this, status is always "warn"


# ---------------------------------------------------------- dataclasses


@dataclass
class RealizedTrade:
    """One closed paper-auto trade reconstructed from its ledger.

    Mirrors enough of :class:`tools.backtest.simulator.TradeOutcome` that
    we can synthesize one for the metrics module.
    """
    ticker: str
    setup_type: str
    setup_grade: Optional[str]
    fill_date: _dt.date
    exit_date: _dt.date
    fill_price: float
    exit_price: float
    initial_stop: float
    shares: int
    r_multiple: float                # (exit - entry) / (entry - initial_stop)
    pnl_pct: float                   # (exit - entry) / entry
    pnl_usd: float                   # (exit - entry) * shares
    bars_held: int                   # trading days, approximated as calendar-day delta
    exit_reason: str = "closed"      # Session 3 may populate; default safe

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fill_date"] = self.fill_date.isoformat()
        d["exit_date"] = self.exit_date.isoformat()
        return d


@dataclass
class SetupComparison:
    """Realized-vs-backtest comparison for one setup type."""
    setup: str
    n_trades: int
    realized_sharpe: Optional[float]
    backtest_sharpe: Optional[float]
    sharpe_delta: Optional[float]                  # realized - backtest
    realized_max_drawdown_pct: Optional[float]
    backtest_max_drawdown_pct: Optional[float]
    max_drawdown_delta: Optional[float]            # realized - backtest (DD is negative)
    status: str                                    # "ok" | "warn" | "fail" | "no_data"
    status_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OpenPosition:
    """One currently-open paper-auto position with unrealized P&L."""
    ticker: str
    setup_type: Optional[str]
    setup_grade: Optional[str]
    entry_price: float
    shares: int
    fill_date: Optional[_dt.date]
    days_open: Optional[int]
    current_stop: Optional[float]
    current_price: Optional[float] = None          # populated by compute_open_pnl
    market_value: Optional[float] = None
    unrealized_pnl_usd: Optional[float] = None
    unrealized_pnl_pct: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.fill_date is not None:
            d["fill_date"] = self.fill_date.isoformat()
        return d


@dataclass
class PerformanceReport:
    """Top-level performance summary for the paper-auto track."""
    asof: str
    setup_filter: Optional[str]
    n_realized: int
    n_open: int
    n_submitted: int
    realized_trades: list[RealizedTrade] = field(default_factory=list)
    open_positions: list[OpenPosition] = field(default_factory=list)
    overall_trade_stats: Optional[TradeStats] = None
    overall_return_stats: Optional[ReturnStats] = None
    by_setup_trade_stats: dict[str, TradeStats] = field(default_factory=dict)
    by_setup_return_stats: dict[str, ReturnStats] = field(default_factory=dict)
    comparisons: list[SetupComparison] = field(default_factory=list)
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asof": self.asof,
            "setup_filter": self.setup_filter,
            "n_realized": self.n_realized,
            "n_open": self.n_open,
            "n_submitted": self.n_submitted,
            "risk_per_trade": self.risk_per_trade,
            "realized_trades": [t.to_dict() for t in self.realized_trades],
            "open_positions": [p.to_dict() for p in self.open_positions],
            "overall_trade_stats": asdict(self.overall_trade_stats) if self.overall_trade_stats else None,
            "overall_return_stats": asdict(self.overall_return_stats) if self.overall_return_stats else None,
            "by_setup_trade_stats": {k: asdict(v) for k, v in self.by_setup_trade_stats.items()},
            "by_setup_return_stats": {k: asdict(v) for k, v in self.by_setup_return_stats.items()},
            "comparisons": [c.to_dict() for c in self.comparisons],
            "notes": self.notes,
        }


# ---------------------------------------------------------- helpers


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _parse_date(v: Any) -> Optional[_dt.date]:
    """Coerce yaml-loaded value (date, datetime, or string) to ``date``."""
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    if isinstance(v, str):
        try:
            return _dt.date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_paper_ledger(path: str) -> Optional[dict[str, Any]]:
    """Load a paper-auto ledger YAML or return None on any failure.

    Performance reporting must never crash on a malformed ledger; failures
    surface in ``PerformanceReport.notes`` instead.
    """
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return None


def _build_realized_trade(
    *,
    ticker: str,
    ledger: dict[str, Any],
    positions_entry: dict[str, Any],
) -> Optional[RealizedTrade]:
    """Build a :class:`RealizedTrade` from a closed paper-auto ledger.

    Returns None if the ledger is missing the fields needed for realized
    stats (most commonly: ``position_state.exit_price`` not yet written —
    Session 3 owns that path).
    """
    meta = ledger.get("meta", {}) or {}
    ps = ledger.get("position_state", {}) or {}
    starter = ps.get("starter", {}) or {}
    sc = ledger.get("setup_classification", {}) or {}

    # Session 3 contract: closed positions have position_state.exit_price.
    # Until merged, this field is absent and the trade is skipped.
    exit_price = _safe_float(ps.get("exit_price"))
    if exit_price is None:
        return None

    fill_price = _safe_float(starter.get("fill_price"))
    initial_stop = _safe_float(starter.get("initial_stop"))
    if fill_price is None or initial_stop is None:
        return None
    if fill_price <= initial_stop:
        # Zero or negative R-denominator — degenerate, skip.
        return None

    shares = int(starter.get("shares") or positions_entry.get("shares") or 0)
    if shares <= 0:
        return None

    fill_date = _parse_date(starter.get("fill_date")) or _parse_date(positions_entry.get("entry_date"))
    exit_date = _parse_date(ps.get("exit_date")) or _parse_date(meta.get("updated_at"))
    if fill_date is None or exit_date is None:
        return None

    r_multiple = (exit_price - fill_price) / (fill_price - initial_stop)
    pnl_pct = (exit_price - fill_price) / fill_price
    pnl_usd = (exit_price - fill_price) * shares
    bars_held = max(1, (exit_date - fill_date).days)

    exit_reason = str(ps.get("exit_reason") or "closed")

    return RealizedTrade(
        ticker=ticker,
        setup_type=str(sc.get("type") or positions_entry.get("setup_type") or "unknown"),
        setup_grade=sc.get("grade") or positions_entry.get("setup_grade"),
        fill_date=fill_date,
        exit_date=exit_date,
        fill_price=fill_price,
        exit_price=exit_price,
        initial_stop=initial_stop,
        shares=shares,
        r_multiple=r_multiple,
        pnl_pct=pnl_pct,
        pnl_usd=pnl_usd,
        bars_held=bars_held,
        exit_reason=exit_reason,
    )


def _build_open_position(
    *,
    ticker: str,
    ledger: dict[str, Any],
    positions_entry: dict[str, Any],
) -> OpenPosition:
    """Build an OpenPosition record from a starter-state paper-auto ledger."""
    meta = ledger.get("meta", {}) or {}
    ps = ledger.get("position_state", {}) or {}
    starter = ps.get("starter", {}) or {}
    sc = ledger.get("setup_classification", {}) or {}

    fill_date = _parse_date(starter.get("fill_date")) or _parse_date(positions_entry.get("entry_date"))
    days_open: Optional[int] = None
    if fill_date is not None:
        days_open = max(0, (_dt.date.today() - fill_date).days)

    return OpenPosition(
        ticker=ticker,
        setup_type=sc.get("type") or positions_entry.get("setup_type"),
        setup_grade=sc.get("grade") or positions_entry.get("setup_grade"),
        entry_price=float(starter.get("fill_price") or positions_entry.get("entry_price") or 0.0),
        shares=int(starter.get("shares") or positions_entry.get("shares") or 0),
        fill_date=fill_date,
        days_open=days_open,
        current_stop=_safe_float(ps.get("current_stop") or starter.get("initial_stop")),
    )


def _trade_to_outcome(t: RealizedTrade) -> TradeOutcome:
    """Convert a RealizedTrade into a synthetic TradeOutcome.

    The :class:`tools.backtest.metrics` helpers operate on TradeOutcome —
    matching their interface lets us reuse the same Sharpe / max-DD code
    path that drives the deployment gate.
    """
    target_price = t.fill_price + 2.0 * (t.fill_price - t.initial_stop)
    signal = TradeSignal(
        ticker=t.ticker,
        setup_type=t.setup_type,
        setup_grade=t.setup_grade or "unknown",
        entry_date=t.fill_date,
        fill_date=t.fill_date,
        entry_price=t.fill_price,
        stop_price=t.initial_stop,
        target_price=target_price,
        max_hold_days=max(t.bars_held, 1),
        atr_at_signal=max(0.01, abs(t.fill_price - t.initial_stop)),
    )
    return TradeOutcome(
        signal=signal,
        exit_date=t.exit_date,
        exit_price=t.exit_price,
        exit_reason=t.exit_reason,
        bars_held=t.bars_held,
        pnl_pct=t.pnl_pct,
        r_multiple=t.r_multiple,
        final_stop=t.initial_stop,
    )


# ---------------------------------------------------------- comparison


def _backtest_expectations(deployable_path: Optional[str] = None) -> dict[str, dict[str, float]]:
    """Return ``{setup_name: {sharpe, max_dd_pct}}`` from deployable_setups.yml.

    Reads ``rolling_agg_sharpe`` + ``rolling_agg_max_dd_pct`` per the file's
    walk-forward aggregate verdict.
    """
    try:
        data = config.load(deployable_path)
    except config.DeployableConfigError:
        return {}
    out: dict[str, dict[str, float]] = {}
    for row in data.get("deployable", []) or []:
        if not isinstance(row, dict):
            continue
        name = row.get("setup")
        if name is None:
            continue
        out[name] = {
            "sharpe": float(row.get("rolling_agg_sharpe") or 0.0),
            "max_dd_pct": float(row.get("rolling_agg_max_dd_pct") or 0.0),
        }
    return out


def _classify_status(
    *,
    realized_sharpe: Optional[float],
    backtest_sharpe: Optional[float],
    n_trades: int,
) -> tuple[str, str]:
    """Return (status, note) per the three-band tolerance defined at module top."""
    if n_trades == 0:
        return "no_data", "no realized trades for this setup yet"
    if n_trades < MIN_TRADES_FOR_VERDICT:
        return "warn", f"n={n_trades} < {MIN_TRADES_FOR_VERDICT} — verdict preliminary"
    if backtest_sharpe is None or backtest_sharpe <= 0:
        return "warn", "no backtest baseline to compare against"
    if realized_sharpe is None:
        return "warn", "realized Sharpe undefined (insufficient variance)"
    ratio = realized_sharpe / backtest_sharpe
    if ratio >= SHARPE_TOLERANCE_OK:
        return "ok", f"realized within {int((1 - SHARPE_TOLERANCE_OK) * 100)}% of backtest"
    if ratio >= SHARPE_TOLERANCE_WARN:
        return "warn", f"realized {ratio:.0%} of backtest — soft edge erosion"
    return "fail", f"realized {ratio:.0%} of backtest — meaningful edge erosion"


def _build_comparison(
    *,
    setup: str,
    trades: list[RealizedTrade],
    backtest: dict[str, dict[str, float]],
    risk_per_trade: float,
) -> SetupComparison:
    bt = backtest.get(setup)
    bt_sharpe = bt["sharpe"] if bt else None
    bt_dd = bt["max_dd_pct"] if bt else None

    if not trades:
        status, note = _classify_status(
            realized_sharpe=None, backtest_sharpe=bt_sharpe, n_trades=0,
        )
        return SetupComparison(
            setup=setup, n_trades=0,
            realized_sharpe=None, backtest_sharpe=bt_sharpe, sharpe_delta=None,
            realized_max_drawdown_pct=None, backtest_max_drawdown_pct=bt_dd,
            max_drawdown_delta=None,
            status=status, status_note=note,
        )

    outcomes = [_trade_to_outcome(t) for t in trades]
    rs = _compute_return_stats(outcomes, risk_per_trade=risk_per_trade)
    realized_sharpe = rs.sharpe_annualised
    realized_dd = rs.max_drawdown_pct

    sharpe_delta = realized_sharpe - bt_sharpe if bt_sharpe is not None else None
    dd_delta = realized_dd - bt_dd if bt_dd is not None else None
    status, note = _classify_status(
        realized_sharpe=realized_sharpe,
        backtest_sharpe=bt_sharpe,
        n_trades=len(trades),
    )
    return SetupComparison(
        setup=setup, n_trades=len(trades),
        realized_sharpe=realized_sharpe,
        backtest_sharpe=bt_sharpe,
        sharpe_delta=sharpe_delta,
        realized_max_drawdown_pct=realized_dd,
        backtest_max_drawdown_pct=bt_dd,
        max_drawdown_delta=dd_delta,
        status=status, status_note=note,
    )


# ---------------------------------------------------------- public API


def compute_performance(
    setup_filter: Optional[str] = None,
    *,
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
    deployable_path: Optional[str] = None,
) -> PerformanceReport:
    """Build the paper-auto performance report.

    Args:
        setup_filter: when set, include only trades whose ``setup_type``
            matches. Open / submitted counts are computed across ALL
            setups regardless.
        risk_per_trade: equity-curve construction fraction (default 1%).
        deployable_path: override path to ``tools/deployable_setups.yml``;
            primarily for tests.

    Returns:
        :class:`PerformanceReport`. Empty (n_realized=0, n_open=0,
        n_submitted=0) when the track has no positions — never raises.
    """
    notes: list[str] = []
    asof = _now_iso()

    positions_data = state.load_positions_json()
    entries: list[dict[str, Any]] = positions_data.get("positions", []) or []

    realized: list[RealizedTrade] = []
    open_positions: list[OpenPosition] = []
    n_submitted = 0
    n_open_total = 0

    for entry in entries:
        ticker = entry.get("ticker")
        if not ticker:
            continue
        stage = (entry.get("stage") or "").lower()

        if stage == "submitted":
            n_submitted += 1
            continue

        ledger = _load_paper_ledger(state.ledger_path(ticker))
        if ledger is None:
            notes.append(f"{ticker}: paper-auto ledger missing or unreadable; skipped")
            continue

        meta_state = ((ledger.get("meta") or {}).get("state") or "").lower()

        if meta_state == "closed":
            t = _build_realized_trade(
                ticker=ticker, ledger=ledger, positions_entry=entry,
            )
            if t is None:
                # Closed-unfilled (DAY-expired) OR Session 3's exit_price
                # writer hasn't merged yet — flag, don't crash.
                notes.append(
                    f"{ticker}: closed ledger has no exit_price (closed-unfilled "
                    f"or Session 3 close-out path not yet merged); excluded from "
                    f"realized stats"
                )
                continue
            realized.append(t)
        elif meta_state in {"starter", "stage-2", "stage-3", "trailing"}:
            n_open_total += 1
            open_positions.append(_build_open_position(
                ticker=ticker, ledger=ledger, positions_entry=entry,
            ))
        elif meta_state == "submitted":
            # Some lifecycles may not have refreshed positions.json stage.
            n_submitted += 1
        else:
            notes.append(f"{ticker}: unexpected meta.state={meta_state!r}; skipped")

    # Setup filter applied AFTER classification — open/submitted counts are
    # track-wide; realized stats narrow to the filter.
    filtered = (
        [t for t in realized if t.setup_type == setup_filter]
        if setup_filter else list(realized)
    )

    # Overall stats (across the filter)
    overall_outcomes = [_trade_to_outcome(t) for t in filtered]
    overall_trade = _compute_trade_stats(overall_outcomes) if overall_outcomes else None
    overall_returns = _compute_return_stats(overall_outcomes, risk_per_trade=risk_per_trade) if overall_outcomes else None

    # Per-setup stats — partition realized (unfiltered) for the dashboard's
    # comparison table; that's where the vs-backtest verdict lives.
    by_setup_trade: dict[str, TradeStats] = {}
    by_setup_returns: dict[str, ReturnStats] = {}
    trades_by_setup: dict[str, list[RealizedTrade]] = {}
    for t in realized:
        trades_by_setup.setdefault(t.setup_type, []).append(t)
    for setup_name, ts in trades_by_setup.items():
        outs = [_trade_to_outcome(t) for t in ts]
        by_setup_trade[setup_name] = _compute_trade_stats(outs)
        by_setup_returns[setup_name] = _compute_return_stats(outs, risk_per_trade=risk_per_trade)

    # Build the comparison rows — one per deployable setup. Setups with
    # no realized trades still get a row (status=no_data) so the table
    # answers "is anything missing?"
    backtest_expect = _backtest_expectations(deployable_path)
    comparison_setups = set(backtest_expect.keys()) | set(trades_by_setup.keys())
    comparisons = sorted(
        (_build_comparison(
            setup=s,
            trades=trades_by_setup.get(s, []),
            backtest=backtest_expect,
            risk_per_trade=risk_per_trade,
        ) for s in comparison_setups),
        key=lambda c: c.setup,
    )

    if filtered:
        notes.append(
            "Sharpe / max-DD computed via trade-sequence equity curve "
            f"(risk_per_trade={risk_per_trade:.0%}); see metrics._equity_curve. "
            "Bar-by-bar intra-trade drawdown is NOT captured — adequate at "
            "n>=30, noisier below."
        )

    return PerformanceReport(
        asof=asof,
        setup_filter=setup_filter,
        n_realized=len(filtered),
        n_open=n_open_total,
        n_submitted=n_submitted,
        realized_trades=filtered,
        open_positions=open_positions,
        overall_trade_stats=overall_trade,
        overall_return_stats=overall_returns,
        by_setup_trade_stats=by_setup_trade,
        by_setup_return_stats=by_setup_returns,
        comparisons=comparisons,
        risk_per_trade=risk_per_trade,
        notes=notes,
    )


def compute_open_pnl(client: Any = None) -> dict[str, Any]:
    """Unrealized P&L on currently-open paper-auto positions.

    Pulls ``positions()`` from the paper account and intersects with the
    paper-auto positions index — only paper-auto-owned tickers are
    counted, NOT every position on the broker account (the same account
    might also hold human-track positions in tests / development).

    Args:
        client: an existing :class:`tools.broker.tiger.TigerClient`. When
            None, constructs a paper-routed client. Use the
            ``_trade_client=`` injection seam for tests.

    Returns:
        ``{"asof": iso, "total_unrealized_pnl_usd": float,
           "total_market_value_usd": float, "by_position": [OpenPosition.to_dict, ...],
           "missing_quotes": [ticker, ...], "error": str | None}``
    """
    asof = _now_iso()
    positions_data = state.load_positions_json()
    entries: list[dict[str, Any]] = positions_data.get("positions", []) or []

    # Restrict to paper-auto positions that are currently open at the broker.
    open_paper_tickers: dict[str, dict[str, Any]] = {}
    for e in entries:
        stage = (e.get("stage") or "").lower()
        if stage in {"starter", "stage-2", "stage-3", "trailing"} and e.get("ticker"):
            open_paper_tickers[str(e["ticker"]).upper()] = e

    if not open_paper_tickers:
        return {
            "asof": asof,
            "total_unrealized_pnl_usd": 0.0,
            "total_market_value_usd": 0.0,
            "by_position": [],
            "missing_quotes": [],
            "error": None,
        }

    if client is None:
        try:
            # Defer import — TigerClient pulls in the tigeropen SDK which
            # may not be installed in lightweight test environments.
            from ..broker.tiger import BrokerConfigError, TigerClient
            client = TigerClient()
        except (ImportError, Exception) as exc:  # noqa: BLE001
            # BrokerConfigError subclasses RuntimeError; bare Exception
            # catches the wider env-setup failure mode.
            return {
                "asof": asof,
                "total_unrealized_pnl_usd": 0.0,
                "total_market_value_usd": 0.0,
                "by_position": [],
                "missing_quotes": sorted(open_paper_tickers.keys()),
                "error": f"broker init: {exc}",
            }

    try:
        out = client.positions().output
    except Exception as exc:  # noqa: BLE001 — surface broker error, never crash dashboard
        return {
            "asof": asof,
            "total_unrealized_pnl_usd": 0.0,
            "total_market_value_usd": 0.0,
            "by_position": [],
            "missing_quotes": sorted(open_paper_tickers.keys()),
            "error": f"broker positions(): {exc}",
        }

    broker_by_symbol = {
        str(p.get("symbol") or "").upper(): p
        for p in out.get("positions", []) or []
    }

    by_position: list[dict[str, Any]] = []
    missing: list[str] = []
    total_pnl = 0.0
    total_mv = 0.0

    for ticker, entry in open_paper_tickers.items():
        ledger = _load_paper_ledger(state.ledger_path(ticker))
        op = _build_open_position(
            ticker=ticker,
            ledger=ledger or {},
            positions_entry=entry,
        )
        broker_pos = broker_by_symbol.get(ticker)
        if broker_pos is None:
            missing.append(ticker)
            by_position.append(op.to_dict())
            continue

        mv = float(broker_pos.get("market_value") or 0.0)
        pnl = float(broker_pos.get("unrealized_pnl") or 0.0)
        qty = float(broker_pos.get("quantity") or op.shares or 0.0)
        current_price = mv / qty if qty > 0 else None

        op.market_value = mv
        op.unrealized_pnl_usd = pnl
        op.current_price = current_price
        cost_basis = op.entry_price * op.shares
        op.unrealized_pnl_pct = pnl / cost_basis if cost_basis > 0 else None

        total_pnl += pnl
        total_mv += mv
        by_position.append(op.to_dict())

    return {
        "asof": asof,
        "total_unrealized_pnl_usd": total_pnl,
        "total_market_value_usd": total_mv,
        "by_position": by_position,
        "missing_quotes": missing,
        "error": None,
    }
