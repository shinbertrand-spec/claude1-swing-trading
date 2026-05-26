"""Autonomous paper-trading entry pipeline.

Composes the deployable-setup filter + portfolio-track limits + Tiger
placement + paper-auto ledger writes into a single ``place_candidate`` call.

Upstream concerns (NOT this module's job):

* Building the candidate (trade-researcher writes the candidate ledger)
* 5-gate compliance check (risk-and-compliance Mode 2)
* Position sizing (tools.position_sizer)

This module assumes the caller has already vetted the trade. It enforces the
*track-level* discipline (deployable filter, paper-auto-track concentration
limits) and performs the broker call + persistence.

Session 1 scope: ``submitted`` state writes only. EOD reconciliation is
Session 2.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Optional

from ..broker.tiger import BrokerConfigError, BrokerOrderError, TigerClient
from ..regime_check import classify_broad
from ..trend_template import compute_from_ticker as tt_from_ticker
from . import config, state

# Hard rules per CLAUDE.md, applied to the paper-auto track in isolation.
MAX_POSITIONS = 8
MAX_PCT_PER_POSITION = 0.05
MAX_PCT_PER_SECTOR = 0.20
MIN_CASH_BUFFER_PCT = 0.15

# Lever D — regime-conditional live sizing (added 2026-05-26).
# The broad-market ticker whose trend-template stage drives the live
# sizing multiplier. Per tools.regime_check.classify_broad:
#   stage_2_confirmed    → 1.00× (full size)
#   stage_2_weakening    → 0.75× (~25% size reduction)
#   stage_3_transitional → 0.50× (half size)
#   stage_4              → 0.00× (halt — refuse new entries)
REGIME_BROAD_TICKER = "SPY"


@dataclass
class CandidateInput:
    """Vetted candidate ready for placement on the paper-auto track."""
    ticker: str
    setup_type: str
    setup_grade: Optional[str]
    pivot_price: float
    limit_price: float
    stop_price: float
    target_price: Optional[float]
    shares: int
    sector_etf: Optional[str] = None
    reasoning_trace: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PlacementResult:
    """Outcome of a single candidate placement attempt."""
    ticker: str
    status: str   # "placed" / "dry_run" / "rejected" / "error"
    reason: Optional[str] = None
    broker_order_id: Optional[int] = None
    ledger_path: Optional[str] = None
    cost_estimate_usd: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _reject(ticker: str, reason: str) -> PlacementResult:
    return PlacementResult(ticker=ticker, status="rejected", reason=reason)


def _check_track_limits(
    *,
    cand: CandidateInput,
    account_net_liq: float,
    existing_positions: list[dict[str, Any]],
    existing_cash: float,
) -> Optional[str]:
    """Return a rejection reason string, or None if all limits pass.

    Checks against the PAPER-AUTO TRACK ONLY — does not look at
    journal/positions.json (human-discretionary positions).
    """
    if account_net_liq <= 0:
        return "account net_liquidation is 0 or unknown — cannot size"

    # Position count
    if len(existing_positions) >= MAX_POSITIONS:
        return f"position count limit hit ({len(existing_positions)} >= {MAX_POSITIONS})"

    # Per-position cap
    cost = cand.shares * cand.limit_price
    pct_position = cost / account_net_liq
    if pct_position > MAX_PCT_PER_POSITION:
        return (
            f"position would exceed {MAX_PCT_PER_POSITION:.0%} cap "
            f"({pct_position:.2%} of net liq)"
        )

    # Sector cap (sum existing same-sector $ plus this new one)
    if cand.sector_etf:
        same_sector_value = sum(
            p.get("shares", 0) * p.get("entry_price", 0)
            for p in existing_positions
            if p.get("sector") == cand.sector_etf
        )
        proposed_sector_value = same_sector_value + cost
        sector_pct = proposed_sector_value / account_net_liq
        if sector_pct > MAX_PCT_PER_SECTOR:
            return (
                f"sector {cand.sector_etf} would exceed {MAX_PCT_PER_SECTOR:.0%} cap "
                f"({sector_pct:.2%} of net liq)"
            )

    # Cash buffer
    cash_after = existing_cash - cost
    cash_pct_after = cash_after / account_net_liq
    if cash_pct_after < MIN_CASH_BUFFER_PCT:
        return (
            f"would breach {MIN_CASH_BUFFER_PCT:.0%} cash buffer "
            f"({cash_pct_after:.2%} after fill)"
        )

    return None


def _resolve_regime_multiplier() -> tuple[str, float]:
    """Lever D — read the current SPY broad-market regime and return
    (stage_class, multiplier).

    Wrapped in its own function so tests can monkeypatch it without
    needing to stub the underlying trend-template fetch (which would
    require synthetic OHLCV for SPY).
    """
    passes_7 = tt_from_ticker(
        REGIME_BROAD_TICKER, include_rs=False,
    ).output["trend_template_passes"]
    return classify_broad(passes_7)


def place_candidate(
    cand: CandidateInput,
    *,
    client: TigerClient | None = None,
    dry_run: bool = False,
) -> PlacementResult:
    """Place one vetted candidate on the paper-auto track.

    Args:
        cand: vetted candidate with setup + entry + stop + sizing already
            computed upstream.
        client: an existing :class:`TigerClient`. When None, constructs a
            paper-routed client.
        dry_run: when True, runs all checks but does NOT call Tiger and
            does NOT write any file. Returns status="dry_run".

    Returns:
        :class:`PlacementResult` describing the outcome.
    """
    # 1. Deployable-setup filter
    if not config.is_deployable(cand.setup_type):
        return _reject(
            cand.ticker,
            f"setup_type {cand.setup_type!r} not on deployable list",
        )

    # 2. Refuse if a paper-auto ledger already exists (don't double-up)
    if state.ledger_exists(cand.ticker):
        return _reject(
            cand.ticker,
            f"paper-auto ledger already exists at {state.ledger_path(cand.ticker)}",
        )

    # 3. Construct / verify the client (paper-only)
    try:
        c = client or TigerClient()  # refuses live by default
    except BrokerConfigError as exc:
        return PlacementResult(
            ticker=cand.ticker, status="error",
            reason=f"broker config: {exc}",
        )

    # 4. Pull account state + existing track positions for the limit checks
    try:
        summary = c.account_summary().output
    except BrokerOrderError as exc:
        return PlacementResult(
            ticker=cand.ticker, status="error",
            reason=f"account_summary: {exc}",
        )

    net_liq = float(summary.get("net_liquidation") or 0.0)
    cash = float(summary.get("cash") or 0.0)
    existing_positions = state.load_positions_json().get("positions", [])

    # 4b. Lever D — regime-conditional sizing. Re-applied here even when
    # the caller already passed a regime-aware sized candidate (quant
    # scanner does this in Step 2b of /auto-paper). Defensive double-
    # application is intentional: if SPY's trend-template stage degrades
    # between scanner-time and place-time (e.g. an intraday breakdown),
    # this is the last check before the broker call. Never widens size —
    # only multiplies down or halts.
    try:
        regime_class, regime_mult = _resolve_regime_multiplier()
    except Exception as exc:
        # Regime check failures are rare (yfinance hiccup) — fail closed:
        # treat as the most-conservative regime and refuse entry.
        return _reject(
            cand.ticker,
            f"regime_check failed: {exc}; refusing entry (fail-closed)",
        )
    if regime_mult <= 0.0:
        return _reject(
            cand.ticker,
            f"SPY regime is {regime_class} (multiplier 0.0) — "
            f"halt new entries per CLAUDE.md circuit breaker",
        )
    if regime_mult < 1.0:
        new_shares = max(1, int(cand.shares * regime_mult))
        if new_shares < cand.shares:
            cand = replace(cand, shares=new_shares)

    reject = _check_track_limits(
        cand=cand,
        account_net_liq=net_liq,
        existing_positions=existing_positions,
        existing_cash=cash,
    )
    if reject is not None:
        return _reject(cand.ticker, reject)

    cost_estimate = cand.shares * cand.limit_price

    if dry_run:
        return PlacementResult(
            ticker=cand.ticker,
            status="dry_run",
            reason=(
                f"would place limit-buy {cand.shares} {cand.ticker} @ ${cand.limit_price:.2f} "
                f"(stop ${cand.stop_price:.2f}; ~${cost_estimate:,.2f})"
            ),
            cost_estimate_usd=cost_estimate,
        )

    # 5. Place the limit order
    try:
        order_entry = c.place_limit_buy(
            symbol=cand.ticker,
            quantity=cand.shares,
            limit_price=cand.limit_price,
        )
    except BrokerOrderError as exc:
        return PlacementResult(
            ticker=cand.ticker, status="error",
            reason=f"place_limit_buy: {exc}",
        )

    order_id = order_entry.output.get("order_id")

    # 6. Write the submitted ledger
    try:
        path = state.write_submitted_ledger(
            ticker=cand.ticker,
            setup_type=cand.setup_type,
            setup_grade=cand.setup_grade,
            pivot_price=cand.pivot_price,
            limit_price=cand.limit_price,
            stop_price=cand.stop_price,
            shares=cand.shares,
            broker_order_id=order_id,
            broker="tiger_paper",
            sector_etf=cand.sector_etf,
            reasoning_trace=cand.reasoning_trace,
        )
    except state.PaperAutoStateError as exc:
        # The order is already at the broker — surface that the ledger
        # write failed so the user can manually reconcile.
        return PlacementResult(
            ticker=cand.ticker,
            status="error",
            reason=(
                f"order #{order_id} placed at broker but ledger write failed: {exc}. "
                f"Manual reconciliation needed."
            ),
            broker_order_id=order_id,
            cost_estimate_usd=cost_estimate,
        )

    # 7. Append to paper-auto positions.json
    try:
        state.append_to_positions_json({
            "ticker": cand.ticker.upper(),
            "ledger_path": path.replace("\\", "/"),
            "entry_date": state._today(),
            "entry_price": cand.limit_price,   # will be reconciled at EOD with avg_fill
            "shares": cand.shares,
            "stop": cand.stop_price,
            "target_1": cand.target_price,
            "sector": cand.sector_etf,
            "broker_order_id": order_id,
            "broker": "tiger_paper",
            "stage": "submitted",
            "setup_type": cand.setup_type,
            "setup_grade": cand.setup_grade,
        })
    except state.PaperAutoStateError as exc:
        return PlacementResult(
            ticker=cand.ticker,
            status="error",
            reason=(
                f"order #{order_id} placed + ledger written but positions.json append "
                f"failed: {exc}. Manual reconciliation needed."
            ),
            broker_order_id=order_id,
            ledger_path=path,
            cost_estimate_usd=cost_estimate,
        )

    return PlacementResult(
        ticker=cand.ticker,
        status="placed",
        broker_order_id=order_id,
        ledger_path=path,
        cost_estimate_usd=cost_estimate,
    )
