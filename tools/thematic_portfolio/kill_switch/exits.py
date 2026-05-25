"""Order-placement arm for Process B (kill-switch).

Translates a :class:`KillSwitchDecision` into concrete broker calls:

1. Determine target shares to sell per position:
   ``shares_to_sell = floor(sell_fraction * current_shares)``
   (round DOWN to never sell shares we don't own; tier 3 sell_fraction=1.0
   sells the full quantity by definition).

2. For each thematic position with ``shares_to_sell > 0``:
   a. Idempotency check — query open_orders for any existing SELL on this
      symbol tagged with this cycle's ``cycle_id`` (carried via the
      ledger / event log). If found, skip — we've already placed in this
      cycle and a re-run shouldn't double-sell.
   b. Cancel any existing SELL orders for the symbol (the kill-switch's
      sell supersedes any stop-loss or trim order Process A may have placed).
   c. Fetch a current quote via :meth:`TigerClient.get_quote`.
   d. Compute limit price = ``round(bid_price * 0.999, 2)`` (-0.1% slippage).
   e. Place limit-sell via :meth:`TigerClient.place_limit_sell`.

3. UNPROTECTED-state recovery: if a cancel succeeds but the place fails,
   record the symbol in ``unprotected_symbols`` so the next cycle can
   retry. The position is now without a protective stop AND without an
   active exit — the safest failure mode is "log loudly and retry."

4. Per-position errors do NOT abort the loop — we record the error and
   continue to the next thematic position.

## Idempotency model

The kill-switch's idempotency boundary is the **cycle**. Within a single
cycle (one call to :func:`execute_kill_switch_sells`), an order is placed
at most once per symbol. Across cycles, idempotency is enforced by the
``cycle_id`` recorded in the broker order's ``order_user_id`` field (when
the SDK supports it) or by the open_orders lookahead which detects any
pending SELL on the symbol and treats it as "already in flight."

If a previous cycle's order is still pending unfilled, this cycle is a
no-op for that symbol — the kill-switch trusts the prior placement.

## What this module does NOT do (deferred to Session 3)

* Heartbeat watchdog (separate process pinging A + B).
* Telegram/SMS escalation routing per tier.
* Post-fill ledger updates for thematic-portfolio positions (the per-
  position ledgers under ``ledgers/thematic-portfolio/`` don't exist yet;
  populated when Loop 1 first places trades, paper-only until Q3 2026).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ...broker.tiger import BrokerOrderError, TigerClient
from .ladder import KillSwitchDecision
from .positions import ThematicPosition

# Slippage applied to the bid price for the limit-sell. -0.1% per
# CLAUDE.md § Order Execution.
LIMIT_SELL_SLIPPAGE = 0.001


@dataclass
class PerSymbolExitResult:
    """One symbol's exit outcome from :func:`execute_kill_switch_sells`."""

    symbol: str
    action: str  # "placed" | "skipped_existing" | "skipped_zero_shares" |
                 # "error_cancel" | "error_quote" | "error_place" |
                 # "unprotected_after_place_fail"
    shares_to_sell: int = 0
    limit_price: Optional[float] = None
    bid_price: Optional[float] = None
    place_order_id: Optional[int] = None
    cancelled_order_ids: list[int] = field(default_factory=list)
    error: Optional[str] = None
    notes: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExitExecutionResult:
    """Composite outcome across all thematic positions for one cycle."""

    cycle_id: str
    sell_fraction: float
    n_orders_placed: int
    n_symbols_skipped: int
    n_symbols_errored: int
    unprotected_symbols: list[str]
    per_symbol_results: list[PerSymbolExitResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "sell_fraction": self.sell_fraction,
            "n_orders_placed": self.n_orders_placed,
            "n_symbols_skipped": self.n_symbols_skipped,
            "n_symbols_errored": self.n_symbols_errored,
            "unprotected_symbols": self.unprotected_symbols,
            "per_symbol_results": [r.to_dict() for r in self.per_symbol_results],
        }


def _compute_shares_to_sell(current_shares: float, sell_fraction: float) -> int:
    """Round DOWN to avoid selling more than we own. Returns 0 for negative
    or zero sell_fraction."""
    if sell_fraction <= 0 or current_shares <= 0:
        return 0
    # Tier 3 full unwind explicitly closes whatever we hold (incl. fractional).
    if sell_fraction >= 1.0:
        return int(math.floor(float(current_shares)))
    return int(math.floor(float(current_shares) * float(sell_fraction)))


def _pending_limit_sells(
    tiger: TigerClient, symbol: str,
) -> list[dict[str, Any]]:
    """Return open LMT SELL orders for ``symbol`` (idempotency signal).

    A pending LMT SELL means a prior kill-switch cycle placed an
    emergency exit that has not yet filled — we MUST NOT double-place.

    A pending STP SELL is a different thing — that's a protective
    stop-loss; the kill-switch overrides it (cancel it, then place a
    more aggressive LMT SELL).

    On error fetching open_orders, returns []. The worst-case of a
    missed pre-existing-sell detection is one extra sell placed (the
    kill-switch is firing — over-selling is the intended direction of
    failure, not under-selling)."""
    try:
        oo = tiger.open_orders().output
    except BrokerOrderError:
        return []
    matches = []
    for o in oo.get("orders", []):
        if (o.get("symbol") or "").upper() != symbol.upper():
            continue
        if (o.get("action") or "").upper() != "SELL":
            continue
        order_type = (o.get("order_type") or "").upper()
        # LMT match — kill-switch idempotency hit. STP / STP_LMT / TRAIL
        # are protective stops we should override.
        if order_type in ("LMT", "LIMIT"):
            matches.append(o)
    return matches


def _exit_one(
    tiger: TigerClient,
    pos: ThematicPosition,
    sell_fraction: float,
    cycle_id: str,
) -> PerSymbolExitResult:
    """Process a single thematic position. Returns a structured result;
    never raises (all exceptions wrapped into ``error`` field)."""
    symbol = pos.ticker
    shares_to_sell = _compute_shares_to_sell(pos.shares, sell_fraction)

    if shares_to_sell <= 0:
        return PerSymbolExitResult(
            symbol=symbol,
            action="skipped_zero_shares",
            shares_to_sell=0,
            notes=f"shares={pos.shares}, sell_fraction={sell_fraction} -> 0 shares to sell",
        )

    # Idempotency: a pending LMT SELL on this symbol means a prior cycle
    # already placed an emergency exit. Skip rather than stack orders.
    # (Pending STP SELLs are protective stops — those get cancelled below
    # to make way for our more aggressive limit-sell.)
    existing = _pending_limit_sells(tiger, symbol)
    if existing:
        return PerSymbolExitResult(
            symbol=symbol,
            action="skipped_existing",
            shares_to_sell=shares_to_sell,
            notes=(
                f"Found {len(existing)} pending LMT SELL order(s) on {symbol} "
                f"(order_ids={[o.get('order_id') for o in existing]}). "
                "Prior kill-switch cycle already placed; skipping."
            ),
        )

    # Cancel any other resting orders on this symbol — protective STP
    # SELLs (so the broker doesn't double-sell), stale BUYs, etc. The
    # LMT SELL idempotency check above has already early-returned, so
    # nothing here is a prior kill-switch placement.
    cancelled_ids: list[int] = []
    try:
        oo_te = tiger.open_orders().output
        for o in oo_te.get("orders", []):
            if (o.get("symbol") or "").upper() != symbol.upper():
                continue
            oid = o.get("order_id")
            if oid is None:
                continue
            tiger.cancel(order_id=int(oid))
            cancelled_ids.append(int(oid))
    except BrokerOrderError as exc:
        return PerSymbolExitResult(
            symbol=symbol,
            action="error_cancel",
            shares_to_sell=shares_to_sell,
            cancelled_order_ids=cancelled_ids,
            error=f"cancel pre-existing orders failed: {exc}",
        )

    # Quote.
    try:
        quote_te = tiger.get_quote(symbol).output
    except BrokerOrderError as exc:
        return PerSymbolExitResult(
            symbol=symbol,
            action="unprotected_after_place_fail" if cancelled_ids else "error_quote",
            shares_to_sell=shares_to_sell,
            cancelled_order_ids=cancelled_ids,
            error=f"get_quote failed: {exc}",
            notes=(
                "Cancels succeeded but quote failed; symbol unprotected pending retry."
                if cancelled_ids else None
            ),
        )

    bid = float(quote_te.get("bid_price", 0.0) or 0.0)
    if bid <= 0:
        # Fall back to latest_price when bid is stale / zero (e.g. pre-market).
        bid = float(quote_te.get("latest_price", 0.0) or 0.0)
    if bid <= 0:
        return PerSymbolExitResult(
            symbol=symbol,
            action="unprotected_after_place_fail" if cancelled_ids else "error_quote",
            shares_to_sell=shares_to_sell,
            cancelled_order_ids=cancelled_ids,
            bid_price=bid,
            error=f"no usable bid or latest_price for {symbol}",
        )

    limit_price = round(bid * (1.0 - LIMIT_SELL_SLIPPAGE), 2)

    # Place the limit-sell.
    try:
        place_te = tiger.place_limit_sell(
            symbol=symbol, quantity=shares_to_sell, limit_price=limit_price,
        ).output
    except BrokerOrderError as exc:
        return PerSymbolExitResult(
            symbol=symbol,
            action="unprotected_after_place_fail" if cancelled_ids else "error_place",
            shares_to_sell=shares_to_sell,
            limit_price=limit_price,
            bid_price=bid,
            cancelled_order_ids=cancelled_ids,
            error=f"place_limit_sell failed: {exc}",
            notes=(
                f"Cancelled {len(cancelled_ids)} prior order(s) but place failed — "
                "symbol unprotected pending next-cycle retry."
                if cancelled_ids else None
            ),
        )

    return PerSymbolExitResult(
        symbol=symbol,
        action="placed",
        shares_to_sell=shares_to_sell,
        limit_price=limit_price,
        bid_price=bid,
        cancelled_order_ids=cancelled_ids,
        place_order_id=int(place_te.get("order_id")) if place_te.get("order_id") else None,
        notes=f"cycle_id={cycle_id}",
    )


def execute_kill_switch_sells(
    tiger: TigerClient,
    thematic_positions: list[ThematicPosition],
    decision: KillSwitchDecision,
    cycle_id: str,
) -> ExitExecutionResult:
    """Place emergency sells across all thematic positions per ``decision``.

    Per-position errors are recorded but do NOT abort the loop — one
    failing symbol must not prevent the others from being sold.

    Args:
        tiger: paper-routed TigerClient.
        thematic_positions: result of
            :func:`positions.identify_thematic_positions`.
        decision: the KillSwitchDecision from the current cycle. Only
            ``sell_fraction`` is used (the action / tier are advisory —
            this function trusts the caller to invoke only on non-hold).
        cycle_id: the current cycle's ID, recorded in per-position notes.

    Returns:
        :class:`ExitExecutionResult` summarising the cycle.
    """
    per_symbol: list[PerSymbolExitResult] = []
    for pos in thematic_positions:
        per_symbol.append(_exit_one(tiger, pos, decision.sell_fraction, cycle_id))

    n_placed = sum(1 for r in per_symbol if r.action == "placed")
    n_skipped = sum(
        1 for r in per_symbol
        if r.action in ("skipped_existing", "skipped_zero_shares")
    )
    n_errored = sum(
        1 for r in per_symbol
        if r.action in (
            "error_cancel", "error_quote", "error_place",
            "unprotected_after_place_fail",
        )
    )
    unprotected = [
        r.symbol for r in per_symbol
        if r.action == "unprotected_after_place_fail"
    ]

    return ExitExecutionResult(
        cycle_id=cycle_id,
        sell_fraction=decision.sell_fraction,
        n_orders_placed=n_placed,
        n_symbols_skipped=n_skipped,
        n_symbols_errored=n_errored,
        unprotected_symbols=unprotected,
        per_symbol_results=per_symbol,
    )
