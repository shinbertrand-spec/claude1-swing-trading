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
from ..contract import TraceEntry
from ..regime_check import classify_broad
from ..trend_template import compute_from_ticker as tt_from_ticker
from . import config, screener, state

# Hard rules per CLAUDE.md, applied to the paper-auto track in isolation.
MAX_POSITIONS = 8
MAX_PCT_PER_POSITION = 0.05
MAX_PCT_PER_SECTOR = 0.20
MIN_CASH_BUFFER_PCT = 0.15

# Terminal stages that are NOT live exposure: a DAY-expired-unfilled order
# (`closed_unfilled`) or a closed-out position (`closed`) must not consume a
# concurrent-position slot or add to sector concentration (fix 2026-06-15 — a
# stale `closed_unfilled` row was prematurely tripping the 8-position cap).
# Deny-list (not allow-list) so anything live OR unknown/missing still counts
# — fail safe toward NOT over-placing. Compared lower-cased.
_CLOSED_STAGES = {"closed", "closed_unfilled"}


def _open_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter a positions.json list to positions that still hold a slot.

    Excludes only the terminal stages ``closed`` / ``closed_unfilled``. A
    position with a missing/unknown stage still counts (fail-safe: an
    unaccountable row should occupy a slot rather than be silently ignored).
    """
    return [
        p for p in positions
        if (p.get("stage") or "").strip().lower() not in _CLOSED_STAGES
    ]

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
    # Phase 3 multi-rater panel — orchestrator sets these after the
    # critic panel runs. sizing_multiplier is in [0.0, 1.0] (0.0=defer);
    # panel_verdict is the full PanelVerdict.to_dict() blob for ledger
    # persistence + Telegram summary.
    sizing_multiplier: float = 1.0
    panel_verdict: Optional[dict[str, Any]] = None
    # Strategy-discovery track (Alfred Delta 6). Sourced from the
    # deployable_setups.yml row's track: field. None = generic (back-compat).
    track: Optional[str] = None


@dataclass
class PlacementResult:
    """Outcome of a single candidate placement attempt."""
    ticker: str
    status: str   # "placed" / "dry_run" / "rejected" / "error"
    reason: Optional[str] = None
    broker_order_id: Optional[int] = None
    ledger_path: Optional[str] = None
    cost_estimate_usd: Optional[float] = None
    screener_trace: Optional[dict[str, Any]] = None  # set when screener ran
    panel_verdict: Optional[dict[str, Any]] = None   # mirrors CandidateInput.panel_verdict
    panel_sizing_applied: bool = False               # True iff sizing_multiplier was applied to shares

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run_screener(ticker: str, claimed_sector_etf: Optional[str]):
    """Indirection so tests can monkeypatch the network-touching screener
    call without stubbing every internal helper. Returns a
    :class:`tools.auto_paper.screener.ScreenerResult`.
    """
    return screener.screen(ticker, claimed_sector_etf=claimed_sector_etf)


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
    apply_panel_sizing: bool = False,
    auto_paper_run_dir: Optional[str] = None,
) -> PlacementResult:
    """Place one vetted candidate on the paper-auto track.

    Args:
        cand: vetted candidate with setup + entry + stop + sizing already
            computed upstream.
        client: an existing :class:`TigerClient`. When None, constructs a
            paper-routed client.
        dry_run: when True, runs all checks but does NOT call Tiger and
            does NOT write any file. Returns status="dry_run".
        apply_panel_sizing: when True, multiply ``cand.shares`` by
            ``cand.sizing_multiplier`` before track-limit checks (Phase 3
            critic-panel sizing). When False (default — **shadow mode**),
            the multiplier is logged in PlacementResult but NOT applied.
            Per Phase 3 scope (2026-05-27), shadow mode runs for 1-2 weeks
            while calibration data accumulates.
        auto_paper_run_dir: when set, the run-directory path produced by
            :func:`tools.auto_paper.run_entry.phase_init` is appended to
            ``cand.reasoning_trace`` so each placement is traceable back to
            its run dir. Pure additive — existing callers (the original
            v1 slash command) leave this None and see no behavior change.

    Returns:
        :class:`PlacementResult` describing the outcome.
    """
    if auto_paper_run_dir is not None:
        # Stamp the run-dir reference on the candidate's reasoning_trace so
        # the paper-auto ledger downstream carries the back-reference.
        # Mutating the list is fine — CandidateInput is a per-placement
        # value, not a long-lived object.
        # Emit a schema-valid trace_step (tool/inputs/output/fetched_at). The
        # `id` is stamped by state._assign_trace_ids at ledger-write time.
        # A bespoke {tool, kind, auto_paper_run_dir} dict was malformed three
        # ways vs $defs.trace_step (extra keys + missing required id/output/
        # fetched_at), which orphaned the broker order on write-validate
        # failure — fixed 2026-06-05.
        cand.reasoning_trace.append(TraceEntry(
            tool="tools.auto_paper.run_entry",
            inputs={"kind": "run_dir_reference"},
            output={"auto_paper_run_dir": str(auto_paper_run_dir)},
        ).to_dict())
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

    # 2b. Pre-placement screener — strategy-blind disqualifiers (litigation,
    # dilution, earnings forward 10d) + sector correction. Runs before
    # broker client construction so a screener-block costs zero broker
    # round-trips. Hard-block reasons surface in PlacementResult.reason
    # with a "screener:" prefix so the auto-paper summary can group them.
    screener_trace_dict: Optional[dict[str, Any]] = None
    try:
        screener_result = _run_screener(cand.ticker, cand.sector_etf)
    except Exception as exc:
        # Fail-OPEN on screener crash — the screener's individual checks
        # already fail-open on their own network/API errors; a top-level
        # exception here is unexpected and should not block placement.
        # The crash is recorded in screener_trace so the operator can audit.
        screener_result = None
        screener_trace_dict = {"crashed": True, "error": str(exc)}

    if screener_result is not None:
        screener_trace_dict = screener_result.to_dict()
        # Sector correction — patch the candidate before any sector-cap
        # accounting downstream. The original claimed sector lives in the
        # screener trace evidence so the operator can see what changed.
        if screener_result.corrected_sector_etf is not None:
            cand = replace(cand, sector_etf=screener_result.corrected_sector_etf)
        if screener_result.blocked:
            blocking = ", ".join(screener_result.blocking_checks)
            # First failing check's reason is the human-readable summary.
            first_block = next(
                (c for c in screener_result.checks if not c.passed
                 and c.check in {"litigation", "dilution", "earnings_blackout"}),
                None,
            )
            detail = first_block.reason if first_block else "see screener_trace"
            return PlacementResult(
                ticker=cand.ticker,
                status="rejected",
                reason=f"screener:{blocking} — {detail}",
                screener_trace=screener_trace_dict,
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
    # Count only genuinely-open positions toward the cap / sector limits —
    # `closed_unfilled` + `closed` rows linger in positions.json but are not
    # live exposure (fix 2026-06-15).
    existing_positions = _open_positions(
        state.load_positions_json().get("positions", [])
    )

    # 4b. Lever D — regime-conditional sizing. This is the SINGLE regime
    # multiplier applied to the final pre-place size: the scanner's
    # position_sizer only scales the *risk-budget* path, so a concentration-
    # cap-bound candidate (the common case for wide-ATR-stop quant setups)
    # arrives here with NO regime haircut — this step applies it. Last check
    # before the broker; never widens, only multiplies down or halts.
    #
    # KNOWN ISSUE (2026-06-15): for a *risk-budget*-bound candidate (tight
    # stop), position_sizer already applied the regime mult, so this re-applies
    # it → double-discount (e.g. 0.56× in stage_2_weakening). None of the
    # current live deployables are risk-bound (all wide-stop → conc-bound), so
    # this is latent. Proper fix = apply regime in exactly one layer for both
    # binding constraints (deferred to a daylight change on this money path).
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

    # 4c. Phase 3 critic-panel sizing modifier — applied AFTER lever-D, BEFORE
    # track-limit check. Composition order matters: if the panel says half-size,
    # we apply that first, then the track-limit check operates on the reduced
    # share count (which is more likely to fit under the 5%/20%/15% caps).
    #
    # The `defer` action (sizing_multiplier=0.0) short-circuits to rejection —
    # the panel said "don't take this trade today; manual review tomorrow."
    #
    # Shadow mode (apply_panel_sizing=False, default): we log the multiplier
    # but do NOT modify shares. Per Phase 3 scope (2026-05-27): the panel runs
    # in shadow for 1-2 weeks while calibration data accumulates before the
    # sizing modifier goes live.
    panel_sizing_applied = False
    if apply_panel_sizing and cand.panel_verdict is not None:
        sm = float(cand.sizing_multiplier)
        if sm <= 0.0:
            return PlacementResult(
                ticker=cand.ticker,
                status="rejected",
                reason=(
                    f"panel:defer — critic panel recommends DEFER "
                    f"(sizing_multiplier=0.0); manual review tomorrow"
                ),
                screener_trace=screener_trace_dict,
                panel_verdict=cand.panel_verdict,
                panel_sizing_applied=True,
            )
        if sm < 1.0:
            new_shares = max(1, int(cand.shares * sm))
            if new_shares < cand.shares:
                cand = replace(cand, shares=new_shares)
                panel_sizing_applied = True

    reject = _check_track_limits(
        cand=cand,
        account_net_liq=net_liq,
        existing_positions=existing_positions,
        existing_cash=cash,
    )
    if reject is not None:
        return _reject(cand.ticker, reject)

    # 4d. Pre-place ledger validation gate (2026-06-05). Build + schema-validate
    # the submitted ledger BEFORE any broker call, so a schema failure aborts
    # cleanly instead of orphaning a live order. This is the load-bearing fix
    # for the 2026-06-04 v2-shadow failure mode (order placed, ledger write
    # threw on validation, order left unledgered + unstopped). Runs in dry_run
    # too, so a dry run surfaces schema breaks without a broker round-trip.
    # broker_order_id is omitted — unknown until fill, optional in the schema,
    # so its post-place addition can't invalidate an already-valid doc.
    try:
        state.validate_submitted_ledger(
            ticker=cand.ticker,
            setup_type=cand.setup_type,
            setup_grade=cand.setup_grade,
            pivot_price=cand.pivot_price,
            limit_price=cand.limit_price,
            stop_price=cand.stop_price,
            shares=cand.shares,
            broker="tiger_paper",
            sector_etf=cand.sector_etf,
            reasoning_trace=cand.reasoning_trace,
        )
    except state.PaperAutoStateError as exc:
        return PlacementResult(
            ticker=cand.ticker,
            status="rejected",
            reason=f"ledger validation failed pre-place (no order placed): {exc}",
            screener_trace=screener_trace_dict,
            panel_verdict=cand.panel_verdict,
            panel_sizing_applied=panel_sizing_applied,
        )

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
            screener_trace=screener_trace_dict,
            panel_verdict=cand.panel_verdict,
            panel_sizing_applied=panel_sizing_applied,
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
        screener_trace=screener_trace_dict,
        panel_verdict=cand.panel_verdict,
        panel_sizing_applied=panel_sizing_applied,
    )
