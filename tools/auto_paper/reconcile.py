"""EOD reconciliation for the paper-auto track.

After the close, this module:

1. Loads all paper-auto positions still in the ``submitted`` state
2. Pulls today's filled + open orders from Tiger
3. For each pending ledger, matches against the broker by
   ``broker_order_id`` and decides:
     * **filled** — order filled completely → state ``submitted`` →
       ``starter``; ``fill_price`` ← broker ``avg_fill_price``; a STP-loss
       order for ``filled_qty`` is placed at ``stop_price`` via
       :meth:`TigerClient.place_stop_loss` and the returned broker order
       id is recorded on the ledger's ``position_state.stop_order_id``
     * **partial** — partial fill (broker filled < requested) → state
       ``submitted`` → ``starter``; ``shares`` shrinks to ``filled_qty``;
       ``fill_price`` ← ``avg_fill_price``; protective stop sized to the
       ACTUAL filled quantity, not the requested
     * **expired** — order not filled today and no longer open (TIF=DAY
       expired at close) → state ``submitted`` → ``closed`` with a note
     * **still_open** — order still open at the broker (rare for DAY but
       happens with pre-market submits) → no state change
     * **no_match** — no matching order at Tiger at all (e.g. cancelled
       manually outside the framework) → no change; flagged in the
       result
4. Updates the per-ticker ledger + ``journal/paper-auto/positions.json``

Session 2 scope (shipped 2026-05-24): submitted → starter / closed
transitions.

Session 3 scope (shipped 2026-05-24): auto-place broker-side STP SELL on
the submitted → starter transition; record the broker order id on the
ledger so the next reconcile (or monitor run) doesn't double-place.
Per-bar sell-decision composer exits live in ``tools.auto_paper.exits``.
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import yaml

from ..broker.tiger import BrokerConfigError, BrokerOrderError, TigerClient
from . import critic_panel, cron_gate, intent_log, orphan_check, state

DEFAULT_LOOKBACK_DAYS = 5

# A status=`intent` write-ahead record (broker order id unknown) is only
# ABANDONED after this many CONSECUTIVE distinct-date sweeps each POSITIVELY
# confirm — with the broker proven alive — that it holds no matching order. One
# empty / soft-failed / blacked-out read must never abandon a genuinely-live
# order (the orphan this whole subsystem exists to prevent).
ABANDON_QUORUM = 2


@dataclass
class ReconcileResult:
    """One ledger's reconciliation outcome."""
    ticker: str
    action: str          # "filled" / "partial" / "expired" / "still_open" / "no_match" / "skipped" / "error"
    broker_order_id: Optional[int] = None
    requested_qty: Optional[int] = None
    filled_qty: Optional[int] = None
    avg_fill_price: Optional[float] = None
    stop_order_id: Optional[int] = None   # Session 3 — broker order id of the protective stop, if placed
    stop_place_error: Optional[str] = None  # Session 3 — non-fatal note when stop placement failed
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _validate_against_schema(doc: dict[str, Any]) -> None:
    """Re-use state.py's schema validator."""
    state._validate_against_schema(doc)  # noqa: SLF001 — module-private helper, intentional


def _update_ledger_filled(
    ticker: str,
    *,
    avg_fill_price: float,
    filled_qty: int,
    requested_qty: int,
) -> str:
    """Mutate the paper-auto ledger: submitted → starter, fill_price ← avg_fill."""
    p = state.ledger_path(ticker)
    if not os.path.isfile(p):
        raise state.PaperAutoStateError(f"no paper-auto ledger for {ticker}")

    with open(p, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    doc.setdefault("meta", {})
    doc["meta"]["state"] = "starter"
    doc["meta"]["updated_by"] = "auto_paper/reconcile"
    doc["meta"]["updated_at"] = _now_iso()

    ps = doc.setdefault("position_state", {})
    starter = ps.setdefault("starter", {})
    starter["fill_price"] = float(avg_fill_price)
    if filled_qty != requested_qty:
        starter["shares"] = int(filled_qty)
        ps["intended_full_shares"] = int(filled_qty)

    _validate_against_schema(doc)

    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)
    return p


def _update_ledger_expired(ticker: str, reason: str) -> str:
    """Mutate the paper-auto ledger: submitted → closed with notes."""
    p = state.ledger_path(ticker)
    if not os.path.isfile(p):
        raise state.PaperAutoStateError(f"no paper-auto ledger for {ticker}")

    with open(p, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    doc.setdefault("meta", {})
    doc["meta"]["state"] = "closed"
    doc["meta"]["updated_by"] = "auto_paper/reconcile"
    doc["meta"]["updated_at"] = _now_iso()

    existing_notes = doc.get("notes", "")
    new_note = f"Order expired unfilled on {_today_iso()}: {reason}"
    doc["notes"] = f"{existing_notes}\n{new_note}".strip() if existing_notes else new_note

    _validate_against_schema(doc)

    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)
    return p


def _update_positions_json_filled(
    ticker: str,
    *,
    avg_fill_price: float,
    filled_qty: int,
    requested_qty: int,
) -> None:
    """Find the ticker in paper-auto positions.json and update its fill state."""
    if not os.path.isfile(state.PAPER_AUTO_POSITIONS_JSON):
        return
    with open(state.PAPER_AUTO_POSITIONS_JSON, encoding="utf-8") as fh:
        data = json.load(fh)
    found = False
    for entry in data.get("positions", []):
        if entry.get("ticker") == ticker.upper():
            entry["entry_price"] = float(avg_fill_price)
            entry["stage"] = "starter"
            if filled_qty != requested_qty:
                entry["shares"] = int(filled_qty)
            found = True
            break
    if not found:
        return
    data["updated"] = _now_iso()
    with open(state.PAPER_AUTO_POSITIONS_JSON, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _update_positions_json_expired(ticker: str) -> None:
    """Mark a positions.json entry as closed-unfilled."""
    if not os.path.isfile(state.PAPER_AUTO_POSITIONS_JSON):
        return
    with open(state.PAPER_AUTO_POSITIONS_JSON, encoding="utf-8") as fh:
        data = json.load(fh)
    for entry in data.get("positions", []):
        if entry.get("ticker") == ticker.upper():
            entry["stage"] = "closed_unfilled"
            break
    data["updated"] = _now_iso()
    with open(state.PAPER_AUTO_POSITIONS_JSON, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _ledger_stop_price(ticker: str) -> float | None:
    """Read the configured stop_price for a ledger.

    Prefer ``position_state.current_stop``; fall back to
    ``setup_classification.stop_price``. Returns None if neither is present.
    """
    p = state.ledger_path(ticker)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    ps = doc.get("position_state") or {}
    if isinstance(ps.get("current_stop"), (int, float)):
        return float(ps["current_stop"])
    sc = doc.get("setup_classification") or {}
    if isinstance(sc.get("stop_price"), (int, float)):
        return float(sc["stop_price"])
    return None


def _existing_stop_order_id(ticker: str) -> int | None:
    p = state.ledger_path(ticker)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    ps = doc.get("position_state") or {}
    sid = ps.get("stop_order_id")
    return int(sid) if isinstance(sid, (int, float)) else None


def _place_broker_stop_on_fill(
    *,
    client: TigerClient,
    ticker: str,
    filled_qty: int,
) -> tuple[int | None, str | None]:
    """Place a STP SELL for the filled quantity at the ledger's stop_price.

    Returns ``(stop_order_id, error_message)``. On success, error_message
    is None. On failure, stop_order_id is None and error_message describes
    the cause (the caller decides whether to surface it — the position
    state transition is NOT rolled back; the stop can be placed manually
    or by the next monitor / reconcile pass).

    Idempotent guard: if the ledger already has a ``stop_order_id``, this
    function skips placement and returns the existing id.
    """
    if filled_qty <= 0:
        return None, f"refusing stop with non-positive qty={filled_qty}"

    existing = _existing_stop_order_id(ticker)
    if existing is not None:
        return existing, None

    stop_price = _ledger_stop_price(ticker)
    if stop_price is None or stop_price <= 0:
        return None, f"ledger has no usable stop_price for {ticker}"

    try:
        entry = client.place_stop_loss(
            symbol=ticker.upper(),
            quantity=filled_qty,
            stop_price=stop_price,
        )
    except BrokerOrderError as exc:
        return None, f"place_stop_loss failed: {exc}"

    sid = entry.output.get("order_id")
    if sid is None:
        return None, "broker returned no order_id for stop"
    sid = int(sid)
    try:
        state.record_stop_order_id(ticker, sid)
    except state.PaperAutoStateError as exc:
        return sid, f"stop placed (id={sid}) but ledger write failed: {exc}"
    return sid, None


def _pending_submitted_ledgers() -> list[dict[str, Any]]:
    """Return the list of paper-auto positions still in ``submitted`` state.

    Reads from positions.json; if missing or empty, returns [].
    """
    data = state.load_positions_json()
    pending = []
    for entry in data.get("positions", []):
        if entry.get("stage") == "submitted" and entry.get("broker_order_id"):
            pending.append(entry)
    return pending


def _pending_close_ledgers() -> list[dict[str, Any]]:
    """Return the list of paper-auto positions in ``pending_close`` state.

    Added 2026-06-02 as part of the exits.py Bug-1 fix (premature close
    before fill confirmation). exits.py now transitions a position to
    ``pending_close`` after placing the limit-sell; this reconciler
    completes the lifecycle.
    """
    data = state.load_positions_json()
    pending = []
    for entry in data.get("positions", []):
        if (entry.get("stage") or "").lower() == "pending_close":
            if entry.get("pending_sell_order_id"):
                pending.append(entry)
    return pending


def _apply_realized_close(
    ticker: str,
    *,
    exit_price: float,
    exit_reason: str,
) -> None:
    """Transition a paper-auto ledger to ``closed`` at a realized exit price,
    clear the resolved order ids, and capture the Phase-3 calibration outcome.

    Shared by the ``pending_close`` -> ``closed`` path (sell-composer / manual
    exits) and the stop-out path (protective STP filled at broker) so BOTH
    realized exits close the ledger identically and BOTH feed calibration.
    """
    p = state.ledger_path(ticker)
    if not os.path.isfile(p):
        raise state.PaperAutoStateError(f"no paper-auto ledger for {ticker}")
    with open(p, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    doc.setdefault("meta", {})
    doc["meta"]["state"] = "closed"
    doc["meta"]["updated_by"] = "auto_paper/reconcile"
    doc["meta"]["updated_at"] = _now_iso()

    ps = doc.setdefault("position_state", {})
    ps.pop("pending_sell_order_id", None)
    ps.pop("stop_order_id", None)  # any resting order is resolved at close
    ps.pop("stop_place_error", None)  # FIX #5 — a closed position is not naked

    existing = doc.get("notes", "")
    new_note = (
        f"Closed by auto_paper/reconcile on {_today_iso()} at ${exit_price:.4f} — "
        f"reason: {exit_reason}"
    )
    doc["notes"] = f"{existing}\n{new_note}".strip() if existing else new_note

    _validate_against_schema(doc)
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)

    # Close the Phase-3 calibration loop: pair this realized exit with the
    # panel verdict that sized the entry. Best-effort — calibration is
    # observational and must NEVER break the trade-lifecycle close.
    try:
        starter = (doc.get("position_state") or {}).get("starter") or {}
        entry_price = starter.get("fill_price")
        stop_price = starter.get("initial_stop")
        shares = starter.get("shares")
        entry_date = (doc.get("meta") or {}).get("created_at", "")[:10]
        if entry_price and stop_price and shares and entry_date:
            critic_panel.record_calibration_outcome(
                ticker,
                entry_price=float(entry_price),
                exit_price=float(exit_price),
                stop_price=float(stop_price),
                shares=int(shares),
                exit_reason=exit_reason,
                entry_date=entry_date,
            )
    except Exception:
        pass


def _update_ledger_closed_from_pending(
    ticker: str,
    *,
    exit_price: float,
    exit_reason: str,
) -> None:
    """Mutate the ledger: pending_close -> closed; clear pending_sell_order_id."""
    _apply_realized_close(
        ticker, exit_price=exit_price,
        exit_reason=f"{exit_reason} (fill confirmed)",
    )


def _revert_ledger_to_starter_from_pending(ticker: str, reason: str) -> None:
    """Mutate the ledger: pending_close -> starter; clear pending_sell_order_id.

    Called when the exit limit-sell expired DAY-unfilled. The protective
    stop is still in place (exits.py never cancelled it), so the position
    reverts to starter with full surveillance restored.
    """
    p = state.ledger_path(ticker)
    if not os.path.isfile(p):
        raise state.PaperAutoStateError(f"no paper-auto ledger for {ticker}")
    with open(p, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    doc.setdefault("meta", {})
    doc["meta"]["state"] = "starter"
    doc["meta"]["updated_by"] = "auto_paper/reconcile"
    doc["meta"]["updated_at"] = _now_iso()

    ps = doc.setdefault("position_state", {})
    ps.pop("pending_sell_order_id", None)

    existing = doc.get("notes", "")
    new_note = (
        f"Reverted pending_close -> starter by auto_paper/reconcile on "
        f"{_today_iso()}: {reason}. Protective stop remains in place."
    )
    doc["notes"] = f"{existing}\n{new_note}".strip() if existing else new_note

    _validate_against_schema(doc)
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)


def _update_positions_json_closed_from_pending(ticker: str) -> None:
    """Remove a confirmed-closed ticker from the paper-auto positions index."""
    if not os.path.isfile(state.PAPER_AUTO_POSITIONS_JSON):
        return
    with open(state.PAPER_AUTO_POSITIONS_JSON, encoding="utf-8") as fh:
        data = json.load(fh)
    data["positions"] = [
        p for p in data.get("positions", [])
        if p.get("ticker") != ticker.upper()
    ]
    data["updated"] = _now_iso()
    with open(state.PAPER_AUTO_POSITIONS_JSON, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _revert_positions_json_to_starter_from_pending(ticker: str) -> None:
    """Flip a positions.json entry from pending_close back to starter."""
    if not os.path.isfile(state.PAPER_AUTO_POSITIONS_JSON):
        return
    with open(state.PAPER_AUTO_POSITIONS_JSON, encoding="utf-8") as fh:
        data = json.load(fh)
    for entry in data.get("positions", []):
        if entry.get("ticker") == ticker.upper():
            entry["stage"] = "starter"
            entry.pop("pending_sell_order_id", None)
            break
    data["updated"] = _now_iso()
    with open(state.PAPER_AUTO_POSITIONS_JSON, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _starter_positions() -> list[dict[str, Any]]:
    """Return all paper-auto positions currently in ``starter`` state."""
    data = state.load_positions_json()
    out: list[dict[str, Any]] = []
    for p in data.get("positions", []):
        if (p.get("stage") or "").lower() == "starter":
            if p.get("ticker"):
                out.append(p)
    return out


def refresh_starter_stops(
    *,
    client: TigerClient,
    open_by_id: dict[Any, dict[str, Any]] | None = None,
    holdings: dict[str, float] | None = None,
    dry_run: bool = False,
) -> list[ReconcileResult]:
    """Ensure every ``starter`` position has a live broker-side STP.

    Tiger paper STP orders are DAY-only (no GTC). After session close they
    cancel themselves. Without this routine, every ``starter`` position is
    unprotected from session-close until the next monitor / reconcile run
    re-arms its stop — and the pre-2026-05-28 reconcile path only placed
    stops on the submitted→starter transition, never on already-starter
    positions. Result: positions get protective stops on day 1 of their
    fill and never again. (Today's smoke test caught this — MXL / GO / VRT
    had stop_order_id in their ledgers but the actual broker orders were
    long gone, DAY-expired during prior overnight cycles.)

    **Naked-short guard (Priority 3 hardening, 2026-06-08).** A STP SELL is
    only ever placed against shares the broker actually holds. If a position
    is ``starter`` in ``positions.json`` but the broker holds <1 share (closed
    externally, or a journal/broker desync), placing a sized SELL stop would
    create naked-short exposure if it triggered — the same incident class as
    the COIN −639 short. Such positions are surfaced as ``not_held`` and NO
    stop is placed. The placed quantity is also clamped to the lesser of the
    journal share count and the broker-held quantity, so a partial-close
    desync never over-stops into a naked tail.

    For each starter position:

    1. Skip with ``not_held`` if the broker holds <1 share (naked-short guard).
    2. Check ``open_by_id`` for the ledger's recorded ``stop_order_id`` (or any
       open STP SELL on the symbol). If found, the stop is live — emit
       ``stop_intact`` and skip.
    3. Otherwise the previous stop is gone (DAY-expired or cancelled). Place a
       fresh STP via :func:`TigerClient.place_stop_loss` sized to
       ``min(journal_shares, broker_held)`` at the ledger's ``current_stop``
       (or the ``stop_price`` fallback), then update the ledger's
       ``stop_order_id`` via :func:`state.record_stop_order_id`.

    Args:
        client: paper-routed :class:`TigerClient`.
        open_by_id: optional pre-fetched ``{order_id: order}`` map (avoids
            a second :func:`TigerClient.open_orders` call when invoked from
            :func:`reconcile_today`). When None, fetches it here.
        holdings: optional ``{ticker: signed_qty}`` broker snapshot for the
            naked-short guard. When None, fetched via ``client.positions()``.
            Test seam.
        dry_run: when True, computes what would be placed without
            calling the broker or touching the ledger.

    Returns:
        list[ReconcileResult] — one entry per starter position. Possible
        actions:

        - ``stop_intact`` — ledger's stop_order_id is in open_orders; no-op
        - ``stop_replaced`` — ledger had no stop_order_id OR it wasn't open
          at broker; placed a fresh STP, ledger updated
        - ``not_held`` — broker holds <1 share; stop NOT placed (naked guard)
        - ``stop_dry_run`` — would have placed a fresh STP (dry_run=True)
        - ``error`` — could not refresh; ``reason`` describes the cause
    """
    starters = _starter_positions()
    if not starters:
        return []

    # Naked-short guard: never place a SELL stop for a position the broker
    # does not actually hold. Fetch holdings once up front.
    if holdings is None:
        try:
            positions = client.positions().output["positions"]
        except BrokerOrderError as exc:
            return [
                ReconcileResult(
                    ticker=p["ticker"], action="error",
                    reason=f"positions fetch: {exc}",
                ) for p in starters
            ]
        holdings = {p["symbol"].upper(): float(p["quantity"]) for p in positions}

    if open_by_id is None:
        try:
            open_entry = client.open_orders()
        except BrokerOrderError as exc:
            return [
                ReconcileResult(
                    ticker=p["ticker"], action="error",
                    reason=f"open_orders fetch: {exc}",
                ) for p in starters
            ]
        open_by_id = {
            o.get("order_id"): o for o in open_entry.output.get("orders", [])
            if o.get("order_id") is not None
        }

    results: list[ReconcileResult] = []
    for entry in starters:
        ticker = entry["ticker"]

        # Naked-short guard: refuse to place a SELL stop the broker can't back.
        # SIGNED, not abs() — a short (qty < 0) must read as not_held. With abs()
        # a −N short reads as +N held and gets armed with a STP SELL that DEEPENS
        # the short (the 2026-07 NFLX naked-short doubling bug).
        held = int(float(holdings.get(ticker.upper(), 0)))
        if held < 1:
            is_short = held <= -1
            results.append(ReconcileResult(
                ticker=ticker, action="not_held",
                reason=(
                    (f"ledger=starter but broker is SHORT {held} — STP SELL NOT placed "
                     "(would DEEPEN the short; long-only invariant). short_anomaly — "
                     "reconciler gates + operator flattens.")
                    if is_short else
                    ("ledger=starter but broker holds <1 share; STP SELL NOT placed "
                     "(would be naked short). Journal/broker desync — stuck-closing / "
                     "pre-session sweep reconciler's domain.")
                ),
            ))
            continue

        ledger_stop_oid = _existing_stop_order_id(ticker)

        # Look up by id AND by (symbol, type=STP, action=SELL) — Tiger's
        # open_orders typedef wraps id in int/float depending on the SDK
        # build, so we cross-check both ways for robustness.
        live = False
        if ledger_stop_oid is not None and ledger_stop_oid in open_by_id:
            live = True
        else:
            # Fallback: ANY open STP SELL on this symbol counts as protection.
            for o in open_by_id.values():
                if (
                    o.get("symbol") == ticker.upper()
                    and o.get("order_type") == "STP"
                    and o.get("action") == "SELL"
                ):
                    live = True
                    break

        if live:
            results.append(ReconcileResult(
                ticker=ticker, action="stop_intact",
                stop_order_id=ledger_stop_oid,
            ))
            continue

        # No live stop — need to place one. Clamp the size to the lesser of the
        # journal share count and the broker-held quantity, so a partial-close
        # desync (broker holds fewer than the journal thinks) never over-stops.
        journal_shares = int(entry.get("shares") or 0)
        shares = min(journal_shares, held) if journal_shares > 0 else held
        if shares <= 0:
            results.append(ReconcileResult(
                ticker=ticker, action="error",
                reason=f"position has non-positive shares={journal_shares}",
            ))
            continue

        stop_price = _ledger_stop_price(ticker)
        if stop_price is None or stop_price <= 0:
            results.append(ReconcileResult(
                ticker=ticker, action="error",
                reason=f"ledger has no usable stop_price",
            ))
            continue

        if dry_run:
            results.append(ReconcileResult(
                ticker=ticker, action="stop_dry_run",
                requested_qty=shares,
                reason=f"would place STP {shares}sh @ ${stop_price:.2f}",
            ))
            continue

        try:
            placed = client.place_stop_loss(
                symbol=ticker.upper(), quantity=shares, stop_price=stop_price,
            )
        except BrokerOrderError as exc:
            results.append(ReconcileResult(
                ticker=ticker, action="error",
                reason=f"place_stop_loss: {exc}",
            ))
            continue

        new_sid = placed.output.get("order_id")
        if new_sid is None:
            results.append(ReconcileResult(
                ticker=ticker, action="error",
                reason="broker returned no order_id for fresh stop",
            ))
            continue
        new_sid = int(new_sid)
        try:
            state.record_stop_order_id(ticker, new_sid)
        except state.PaperAutoStateError as exc:
            # Stop is live at broker but ledger write failed; surface so the
            # operator can fix manually. Don't roll back the order.
            results.append(ReconcileResult(
                ticker=ticker, action="stop_replaced",
                stop_order_id=new_sid, requested_qty=shares,
                stop_place_error=f"ledger update: {exc}",
            ))
            continue

        results.append(ReconcileResult(
            ticker=ticker, action="stop_replaced",
            stop_order_id=new_sid, requested_qty=shares,
            reason=f"replaced expired stop at ${stop_price:.2f}",
        ))

    return results


# ---------------------------------------------------------------------------
# Post-RTH stuck-closing reconciler (Mode A fix) + orphan discovery (Mode B)
# ---------------------------------------------------------------------------


def _flip_to_starter_from_closed(ticker: str, *, reason: str) -> None:
    """Mode A: flip a {closed, pending_close} ledger back to ``starter``.

    The position's exit DAY-order expired unfilled, so the broker still holds it
    and the close never really happened. Clears any bogus exit_price/exit_reason,
    drops a stale pending_sell_order_id, and appends an audit note to the
    top-level ``notes`` string (schema-safe; same field
    :func:`_revert_ledger_to_starter_from_pending` uses).
    """
    p = state.ledger_path(ticker)
    if not os.path.isfile(p):
        raise state.PaperAutoStateError(f"no paper-auto ledger for {ticker}")
    with open(p, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    doc.setdefault("meta", {})
    doc["meta"]["state"] = "starter"
    doc["meta"]["updated_by"] = "auto_paper/post_rth_reconciler"
    doc["meta"]["updated_at"] = _now_iso()

    ps = doc.setdefault("position_state", {})
    ps.pop("exit_price", None)          # the close was never filled
    ps.pop("exit_reason", None)
    ps.pop("pending_sell_order_id", None)
    # FIX #5 — drop any stale naked flag from a prior failed close; refresh_starter_stops
    # re-arms the stop (and re-sets the flag if that placement fails again).
    ps.pop("stop_place_error", None)

    existing = doc.get("notes", "")
    note = (
        f"[{_today_iso()}] post_rth_reconciler: DAY order expired unfilled; "
        f"broker still holds -> reverted closed/pending_close to starter. {reason}"
    )
    doc["notes"] = f"{existing}\n{note}".strip() if existing else note

    _validate_against_schema(doc)
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)


def _upsert_positions_json_starter(ticker: str, shares: int) -> None:
    """Re-add a flipped position to positions.json as ``starter`` so the monitor
    + stop-refresh resume managing it. Fields are derived from the ledger."""
    doc = state.load_ledger(ticker)
    meta = doc.get("meta") or {}
    ps = doc.get("position_state") or {}
    sc = doc.get("setup_classification") or {}
    st = ps.get("starter") or {}
    entry = {
        "ticker": ticker.upper(),
        "ledger_path": state.ledger_path(ticker),
        "entry_date": st.get("fill_date") or (meta.get("created_at") or "")[:10],
        "entry_price": st.get("fill_price"),
        "shares": int(shares),
        "stop": _ledger_stop_price(ticker),
        "target_1": None,
        "sector": sc.get("sector_etf") or (doc.get("regime") or {}).get("sector_etf"),
        "broker_order_id": st.get("broker_order_id"),
        "broker": st.get("broker", "tiger_paper"),
        "stage": "starter",
        "setup_type": sc.get("type"),
        "setup_grade": sc.get("grade"),
    }
    path = state.PAPER_AUTO_POSITIONS_JSON
    data: dict[str, Any] = {"positions": []}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    positions = [p for p in data.get("positions", []) if p.get("ticker") != ticker.upper()]
    positions.append(entry)
    data["positions"] = positions
    data["updated"] = _now_iso()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)


def _ensure_stop_for(
    ticker: str, qty: int, *, open_by_id: dict[Any, dict[str, Any]],
    client: TigerClient,
) -> tuple[int | None, str | None]:
    """Ensure a live protective STP exists for a (re-)started position.

    Returns ``(stop_order_id, error_or_flag)``. If a live STP already covers the
    symbol, returns its id with no error. If none and the ledger has a usable
    stop_price, places one. If no stop_price is available, returns
    ``(None, "<flag>")`` for operator review (never silently leaves it unstopped
    without flagging).
    """
    sid = _existing_stop_order_id(ticker)
    live = sid is not None and sid in open_by_id
    if not live:
        for o in open_by_id.values():
            if (o.get("symbol") == ticker.upper()
                    and o.get("order_type") == "STP"
                    and o.get("action") == "SELL"):
                live = True
                break
    if live:
        return sid, None

    stop_price = _ledger_stop_price(ticker)
    if stop_price is None or stop_price <= 0:
        return None, "no usable stop_price in ledger; FLAGGED for operator review"
    try:
        placed = client.place_stop_loss(
            symbol=ticker.upper(), quantity=qty, stop_price=stop_price,
        )
    except BrokerOrderError as exc:
        return None, f"place_stop_loss failed: {exc}"
    new_sid = placed.output.get("order_id")
    if new_sid is None:
        return None, "broker returned no order_id for stop"
    new_sid = int(new_sid)
    try:
        state.record_stop_order_id(ticker, new_sid)
    except state.PaperAutoStateError as exc:
        return new_sid, f"stop placed but ledger update failed: {exc}"
    return new_sid, None


def _record_stop_place_error(ticker: str, err: str) -> None:
    """Record a stop-placement failure on a paper-auto ledger so a NAKED held
    starter (state==starter, no stop_order_id) is surfaced on the health
    check's pageable surface instead of sitting unprotected + silent."""
    p = state.ledger_path(ticker)
    if not os.path.isfile(p):
        return
    try:
        with open(p, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        ps = doc.setdefault("position_state", {})
        ps["stop_place_error"] = str(err)[:200]
        doc.setdefault("meta", {})["updated_at"] = _now_iso()
        _validate_against_schema(doc)
        with open(p, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, sort_keys=False)
    except Exception:  # noqa: BLE001 — best-effort surfacing; never crash recovery
        pass


def _persist_orphan_discovery(
    orphans: list[str],
    holdings: dict[str, float],
    *,
    source: str = "post_rth_reconciler",
    corrupt: Optional[list[str]] = None,
    shorts: Optional[list[str]] = None,
) -> str:
    """Write journal/paper-auto/orphan_discovery_<date>.yml. Returns the path.

    Shared by the post-RTH reconciler (``source=post_rth_reconciler``) and the
    pre-session sweep (``source=presession_sweep``). One file per day; the later
    writer wins (the gate payload also records ``source`` + ``discovery_file``).
    ``corrupt`` lists held tickers whose ledger is unparseable — surfaced for the
    pre-session sweep, which (unlike the post-RTH reconciler) treats a corrupt
    held ledger as a gate-worthy uncertain state.
    """
    d = os.path.dirname(state.PAPER_AUTO_POSITIONS_JSON)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"orphan_discovery_{_today_iso()}.yml")
    doc = {
        "timestamp": _now_iso(),
        "source": source,
        "orphans": {t: holdings.get(t) for t in orphans},
        "note": (
            "Broker holds these with NO paper-auto ledger (Mode B). NOT "
            "auto-closed. Operator must reconcile (onboard or flatten), then "
            "clear the cron gate (tools.auto_paper.cron_gate.clear_gate)."
        ),
    }
    if shorts:
        doc["short_anomaly"] = {t: holdings.get(t) for t in shorts}
        doc["short_note"] = (
            "Broker is SHORT these on a LONG-ONLY system (naked-short anomaly). "
            "NOT auto-closed and NEVER given a SELL stop (a SELL deepens a short). "
            "Operator must flatten (BUY to close), then clear the cron gate."
        )
    if corrupt:
        doc["corrupt_held"] = {t: holdings.get(t) for t in corrupt}
        doc["corrupt_note"] = (
            "Broker holds these but the paper-auto ledger is unparseable. "
            "Fix the YAML (or onboard/flatten), then clear the gate."
        )
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)
    return path


def reconcile_stuck_closing(
    *,
    client: TigerClient,
    holdings: dict[str, float] | None = None,
    dry_run: bool = False,
) -> list[ReconcileResult]:
    """Post-RTH Mode-A reconciler + Mode-B orphan discovery.

    **Mode A (stuck-closing):** for every ledger in {closed, pending_close} that
    the broker STILL holds (>= 1 share) -- the DAY exit-order expired unfilled --
    flip ``meta.state`` back to ``starter``, re-add it to positions.json, and
    ensure a protective STP is live (re-place at the ledger stop_price, or flag if
    none).

    **Mode B (orphan discovery):** for every broker holding with NO ledger at all
    (and not protected), log to ``journal/paper-auto/orphan_discovery_<date>.yml``,
    set the cron gate (so the entry pipeline refuses to place until acknowledged),
    and surface an alert. Does NOT auto-close -- the operator decides.

    ``starter``-held positions are healthy (no action). ``submitted``-held are
    reconcile_today's domain (a fill it transitions to starter). ``corrupt``-held
    ledgers are surfaced for manual fix, never flipped or orphan-flagged.

    Args:
        client: paper-routed TigerClient.
        holdings: optional ``{ticker: signed_qty}`` snapshot (else fetched). Test seam.
        dry_run: compute + report without mutating ledgers / positions.json /
            broker / gate.

    Returns:
        list[ReconcileResult]. Actions: ``reverted_to_starter`` / ``stuck_dry_run``
        / ``orphan_discovered`` / ``orphan_dry_run`` / ``corrupt_ledger`` / ``error``.
    """
    if holdings is None:
        try:
            positions = client.positions().output["positions"]
        except BrokerOrderError as exc:
            return [ReconcileResult(ticker="*", action="error",
                                    reason=f"positions fetch: {exc}")]
        holdings = {p["symbol"].upper(): float(p["quantity"]) for p in positions}

    scan = orphan_check.scan_ledgers()
    cls = orphan_check.classify_holdings(holdings, scan=scan)
    results: list[ReconcileResult] = []

    # Open orders for stop liveness checks (best-effort; absence => re-place).
    open_by_id: dict[Any, dict[str, Any]] = {}
    if cls.stuck_closing and not dry_run:
        try:
            oo = client.open_orders().output.get("orders", [])
            open_by_id = {o.get("order_id"): o for o in oo
                          if o.get("order_id") is not None}
        except BrokerOrderError:
            open_by_id = {}

    # --- Short anomaly: long-only invariant broken (broker is SHORT). Gate +
    # surface, NEVER flip to starter, NEVER place a SELL stop — a SELL on a short
    # DEEPENS it (the 2026-07 NFLX doubling bug). Mirrors Mode B (alert + gate,
    # never auto-act); the operator flattens by BUYing to close. ---
    if cls.short_anomaly:
        if not dry_run:
            disc = _persist_orphan_discovery(
                [], holdings, source="short_anomaly_reconciler",
                shorts=cls.short_anomaly,
            )
            cron_gate.set_gate(
                reason="short_anomaly",
                payload={
                    "short_anomaly": cls.short_anomaly,
                    "quantities": {t: holdings.get(t) for t in cls.short_anomaly},
                    "discovery_file": disc,
                },
            )
        for ticker in cls.short_anomaly:
            results.append(ReconcileResult(
                ticker=ticker,
                action=("short_anomaly_dry_run" if dry_run else "short_anomaly"),
                filled_qty=int(float(holdings.get(ticker.upper(), 0))),  # signed
                reason=("broker is SHORT on a long-only system; NO flip / NO stop. "
                        "Logged + cron GATED; operator must flatten (BUY to close)."),
            ))

    # --- Mode A: stuck-closing flip-back ---
    for ticker in cls.stuck_closing:
        signed = float(holdings.get(ticker.upper(), 0))
        if signed < 1:
            # Defense-in-depth: classify_holdings already keeps shorts out of
            # stuck_closing, but never flip / size a stop off a non-long qty.
            results.append(ReconcileResult(
                ticker=ticker,
                action=("short_anomaly" if signed <= -1 else "not_held"),
                reason="stuck_closing candidate is not a long position; refusing flip-back",
            ))
            continue
        qty = int(signed)
        if dry_run:
            results.append(ReconcileResult(
                ticker=ticker, action="stuck_dry_run", filled_qty=qty,
                reason="would flip closed/pending_close -> starter + ensure stop",
            ))
            continue
        try:
            _flip_to_starter_from_closed(ticker, reason=f"broker holds {qty}sh")
            _upsert_positions_json_starter(ticker, qty)
        except Exception as exc:  # ledger/json mutation failure -> surface, continue
            results.append(ReconcileResult(
                ticker=ticker, action="error",
                reason=f"flip-back failed: {exc!r}",
            ))
            continue
        sid, stop_err = _ensure_stop_for(
            ticker, qty, open_by_id=open_by_id, client=client,
        )
        results.append(ReconcileResult(
            ticker=ticker, action="reverted_to_starter", filled_qty=qty,
            stop_order_id=sid, stop_place_error=stop_err,
            reason="DAY order expired unfilled; reverted to starter",
        ))

    # --- Mode B: orphan discovery (alert + gate; never auto-close) ---
    if cls.orphans:
        if not dry_run:
            disc = _persist_orphan_discovery(cls.orphans, holdings)
            cron_gate.set_gate(
                reason="orphan_discovery",
                payload={"orphans": cls.orphans, "discovery_file": disc},
            )
        for ticker in cls.orphans:
            results.append(ReconcileResult(
                ticker=ticker,
                action=("orphan_dry_run" if dry_run else "orphan_discovered"),
                filled_qty=int(float(holdings[ticker])),  # signed (orphans are longs; cosmetic)
                reason=("broker holds; NO ledger (Mode B). Logged + cron GATED; "
                        "operator must reconcile."),
            ))

    # --- corrupt-held: surface only ---
    for ticker in cls.corrupt_held:
        results.append(ReconcileResult(
            ticker=ticker, action="corrupt_ledger",
            reason="broker holds but ledger unparseable; manual fix required",
        ))

    return results


@dataclass
class PresessionSweep:
    """Result of the pre-session orphan sweep (Priority 2 — Mode-B defense-in-depth).

    Read-only with respect to the broker and ledgers — it NEVER flips, places, or
    closes. Its only side effects (when not ``dry_run`` and something gate-worthy
    is found) are persisting a discovery file and setting the cron gate.
    """
    holdings: dict[str, float]
    healthy: list[str]
    orphans: list[str]            # Mode B — no ledger at all (gate-worthy)
    corrupt_held: list[str]       # unparseable ledger on a held position (gate-worthy)
    stuck_closing: list[str]      # Mode A — reconciler's domain (surfaced, NOT gated on)
    submitted_held: list[str]     # a fill reconcile_today owns (surfaced only)
    gated_now: bool               # this sweep set the gate
    discovery_path: Optional[str]
    skipped: bool = False         # could not fetch broker holdings
    skip_reason: Optional[str] = None
    dry_run: bool = False
    short_anomaly: list[str] = field(default_factory=list)  # broker SHORT (gate-worthy)

    @property
    def gate_tickers(self) -> list[str]:
        return sorted(set(self.orphans) | set(self.corrupt_held))


def presession_sweep(
    *,
    client: TigerClient | None = None,
    holdings: dict[str, float] | None = None,
    dry_run: bool = False,
) -> PresessionSweep:
    """Pre-session (morning, before any placement) orphan sweep — Priority 2.

    Defense-in-depth for Failure Mode B: the post-RTH reconciler
    (:func:`reconcile_stuck_closing`) already discovers orphans + sets the gate
    at market close, which protects the NEXT morning's entry via the
    ``run_entry.phase_init`` gate check. But if that reconciler never ran
    (machine off, holiday, crash), the morning entry would proceed on a STALE
    gate state with no fresh check. This sweep runs a fresh, READ-ONLY orphan
    check at the top of the entry flow so the bot self-protects regardless.

    Gates on **true orphans (Mode B)** and **corrupt-held ledgers** — both are
    "broker state we cannot account for", so placing new trades on top would
    mis-count the concurrent-position cap and pile onto an unknown position.
    Does NOT gate on ``stuck_closing`` (Mode A — the post-RTH reconciler flips
    those) or ``submitted_held`` (a fill ``reconcile_today`` owns); those are
    surfaced for logging only.

    Never auto-closes, never flips, never places. Operator clears the gate after
    reconciling (``cron_gate.clear_gate``).

    Args:
        client: paper-routed TigerClient (required unless ``holdings`` given).
        holdings: optional ``{ticker: signed_qty}`` snapshot. Test seam.
        dry_run: detect + report but do not persist discovery or set the gate.

    Returns:
        PresessionSweep. On broker-fetch failure returns ``skipped=True`` (the
        sweep is best-effort; the downstream ``account_summary`` call is the
        hard broker dependency).
    """
    if holdings is None:
        if client is None:
            raise ValueError("presession_sweep requires `client` or `holdings`")
        try:
            positions = client.positions().output["positions"]
        except BrokerOrderError as exc:
            return PresessionSweep(
                holdings={}, healthy=[], orphans=[], corrupt_held=[],
                stuck_closing=[], submitted_held=[], gated_now=False,
                discovery_path=None, skipped=True,
                skip_reason=f"positions fetch failed: {exc}", dry_run=dry_run,
            )
        holdings = {p["symbol"].upper(): float(p["quantity"]) for p in positions}

    scan = orphan_check.scan_ledgers()
    cls = orphan_check.classify_holdings(holdings, scan=scan)

    gated_now = False
    discovery_path: Optional[str] = None
    if (cls.orphans or cls.corrupt_held or cls.short_anomaly) and not dry_run:
        discovery_path = _persist_orphan_discovery(
            cls.orphans, holdings,
            source="presession_sweep", corrupt=cls.corrupt_held,
            shorts=cls.short_anomaly,
        )
        cron_gate.set_gate(
            reason="presession_orphan_sweep",
            payload={
                "orphans": cls.orphans,
                "corrupt_held": cls.corrupt_held,
                "short_anomaly": cls.short_anomaly,
                "discovery_file": discovery_path,
            },
        )
        gated_now = True

    return PresessionSweep(
        holdings={k.upper(): v for k, v in holdings.items()},
        healthy=cls.healthy,
        orphans=cls.orphans,
        corrupt_held=cls.corrupt_held,
        stuck_closing=cls.stuck_closing,
        submitted_held=cls.submitted_held,
        gated_now=gated_now,
        discovery_path=discovery_path,
        dry_run=dry_run,
        short_anomaly=cls.short_anomaly,
    )


def reconcile_stop_outs(
    *,
    client: TigerClient,
    filled_by_id: dict[Any, dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> list[ReconcileResult]:
    """Detect + close ``starter`` positions whose protective STP filled.

    A ``starter`` position whose recorded ``stop_order_id`` appears in the
    broker's FILLED list means the protective stop triggered (price hit the
    stop). Without this the ledger stays stale in ``starter`` forever, the
    realized loss never reaches calibration, and the next
    :func:`refresh_starter_stops` pass re-arms a STP on a position we no longer
    hold. Run this BEFORE refresh on every reconcile / monitor pass.

    For each stopped-out position: close the ledger at the stop's
    ``avg_fill_price`` (via :func:`_apply_realized_close`, which also records
    the calibration outcome) and remove it from positions.json.

    Args:
        client: paper-routed :class:`TigerClient`.
        filled_by_id: optional pre-fetched ``{order_id: order}`` map of FILLED
            orders (avoids a second ``get_filled_orders`` when called from
            :func:`reconcile_today`). Fetched here when None.
        dry_run: when True, reports what would close without writing or closing.

    Returns:
        list[ReconcileResult] — one per starter that actually stopped out
        (``stopped_out`` / ``stop_out_dry_run`` / ``error``). Positions whose
        stop is still resting produce no result.
    """
    starters = _starter_positions()
    if not starters:
        return []

    if filled_by_id is None:
        start = (_dt.date.today() - _dt.timedelta(days=DEFAULT_LOOKBACK_DAYS)).isoformat()
        end = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
        try:
            filled_entry = client.get_filled_orders(start_time=start, end_time=end)
        except BrokerOrderError as exc:
            return [
                ReconcileResult(ticker=p["ticker"], action="error",
                                reason=f"get_filled_orders: {exc}")
                for p in starters
            ]
        filled_by_id = {
            o.get("order_id"): o for o in filled_entry.output.get("orders", [])
            if o.get("order_id") is not None
        }

    results: list[ReconcileResult] = []
    for entry in starters:
        ticker = entry["ticker"]
        sid = _existing_stop_order_id(ticker)
        if sid is None:
            continue
        filled = filled_by_id.get(sid)
        if filled is None:
            continue  # stop still resting / not filled — no-op

        exit_price = filled.get("avg_fill_price")
        filled_qty = int(filled.get("filled_quantity") or 0)
        if exit_price is None or filled_qty <= 0:
            results.append(ReconcileResult(
                ticker=ticker, action="error", stop_order_id=sid,
                reason=f"stop in filled list but missing qty/avg_fill: {filled}",
            ))
            continue

        if dry_run:
            results.append(ReconcileResult(
                ticker=ticker, action="stop_out_dry_run", stop_order_id=sid,
                avg_fill_price=float(exit_price), filled_qty=filled_qty,
                reason=f"would close — protective stop filled @ ${float(exit_price):.4f}",
            ))
            continue

        try:
            _apply_realized_close(
                ticker, exit_price=float(exit_price),
                exit_reason="protective stop filled at broker",
            )
            _update_positions_json_closed_from_pending(ticker)
        except state.PaperAutoStateError as exc:
            results.append(ReconcileResult(
                ticker=ticker, action="error", stop_order_id=sid,
                reason=f"stop-out close: {exc}",
            ))
            continue

        results.append(ReconcileResult(
            ticker=ticker, action="stopped_out", stop_order_id=sid,
            avg_fill_price=float(exit_price), filled_qty=filled_qty,
            reason="protective stop filled at broker",
        ))
    return results


@dataclass
class IntentReconcileResult:
    """Outcome of resolving one write-ahead intent (intent_log)."""
    client_order_id: str
    ticker: str
    action: str  # ledgered_recovered / ledgered_already / abandoned / still_pending / error / dry_run
    broker_order_id: Optional[int] = None
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _claimed_order_ids() -> set[int]:
    """Broker order ids already referenced by an existing paper-auto ledger.

    Used so the intent recovery sweep never adopts a broker order that is
    already ledgered under a different ticker / intent.
    """
    claimed: set[int] = set()
    scan = orphan_check.scan_ledgers()
    for doc in scan.docs.values():
        ps = (doc.get("position_state") or {})
        starter = (ps.get("starter") or {})
        for oid in (starter.get("broker_order_id"), ps.get("stop_order_id")):
            if isinstance(oid, (int, float)):
                claimed.add(int(oid))
    return claimed


def _reconstruct_ledger_from_intent(rec: "intent_log.IntentRecord", order_id: int | None) -> str:
    """Write the canonical submitted ledger + positions.json row from a durable
    intent — the recovery for a crash between place and ledger-write.

    Idempotent at the call site: callers check ``state.ledger_exists`` first.
    """
    path = state.write_submitted_ledger(
        ticker=rec.ticker,
        setup_type=rec.setup_type,
        setup_grade=rec.setup_grade,
        pivot_price=rec.pivot_price,
        limit_price=rec.limit_price,
        stop_price=rec.stop_price,
        shares=rec.shares,
        broker_order_id=order_id,
        broker=rec.broker or "tiger_paper",
        sector_etf=rec.sector_etf,
        reasoning_trace=rec.reasoning_trace,
    )
    # positions.json row (append is a no-op-safe: refuses dup ticker, which we
    # guard against by only calling when the ticker is absent).
    pj = state.load_positions_json()
    have = {p.get("ticker") for p in pj.get("positions", [])}
    if rec.ticker.upper() not in have:
        state.append_to_positions_json({
            "ticker": rec.ticker.upper(),
            "ledger_path": path.replace("\\", "/"),
            "entry_date": _today_iso(),
            "entry_price": rec.limit_price,
            "shares": rec.shares,
            "stop": rec.stop_price,
            "target_1": rec.target_price,
            "sector": rec.sector_etf,
            "broker_order_id": order_id,
            "broker": rec.broker or "tiger_paper",
            "stage": "submitted",
            "setup_type": rec.setup_type,
            "setup_grade": rec.setup_grade,
        })
    return path


def _account_alive(client: "TigerClient | None") -> bool:
    """Positive proof the broker view is trustworthy THIS sweep: a successful
    account-summary returning real data (non-zero net-liq or cash). Used to
    distinguish a genuinely-empty live account (abandonable) from a broker
    blackout / soft-fail (never abandon). Returns False if we cannot prove it
    (no client, or the call errors)."""
    if client is None:
        return False
    try:
        out = client.account_summary().output
    except BrokerOrderError:
        return False
    try:
        return float(out.get("net_liquidation") or 0.0) > 0.0 or float(out.get("cash") or 0.0) > 0.0
    except (TypeError, ValueError):
        return False


def _human_track_order_ids() -> set[int]:
    """Broker order ids belonging to the HUMAN-discretionary track.

    Origin guard for the intent recovery sweep: the paper-auto sweep must NEVER
    adopt a human-track order that happens to share (symbol, qty, limit). Scans
    ``journal/positions.json`` + ``ledgers/positions/*.yml`` for any recorded
    broker order id. Best-effort + defensive — a read failure yields an empty
    set (the strict exact-match + uniqueness guards still apply)."""
    ids: set[int] = set()

    def _add(v: Any) -> None:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            ids.add(int(v))

    try:
        if os.path.isfile("journal/positions.json"):
            with open("journal/positions.json", encoding="utf-8") as fh:
                doc = json.load(fh)
            for p in (doc.get("positions") or []):
                if isinstance(p, dict):
                    _add(p.get("broker_order_id"))
                    el = p.get("entry_leg") or {}
                    if isinstance(el, dict):
                        _add(el.get("broker_order_id"))
    except Exception:  # noqa: BLE001 — never let the guard crash the sweep
        pass

    try:
        for fp in glob.glob(os.path.join("ledgers/positions", "*.yml")):
            try:
                with open(fp, encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh) or {}
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(doc, dict):
                continue
            ps = doc.get("position_state") or {}
            if isinstance(ps, dict):
                _add(ps.get("stop_order_id"))
                for legname in ("starter", "stage_2", "stage_3"):
                    leg = ps.get(legname) or {}
                    if isinstance(leg, dict):
                        _add(leg.get("broker_order_id"))
            el = doc.get("entry_leg") or {}
            if isinstance(el, dict):
                _add(el.get("broker_order_id"))
    except Exception:  # noqa: BLE001
        pass
    return ids


def _match_open_or_filled(
    rec: "intent_log.IntentRecord",
    *,
    open_orders: list[dict[str, Any]],
    filled_orders: list[dict[str, Any]],
    claimed: set[int],
    human_ids: set[int] | None = None,
) -> tuple[int | None, str]:
    """Find the broker order matching an intent whose broker_order_id is unknown
    (status=intent — crash before the intent could record the id).

    Returns ``(order_id, status)`` where status is:
      * ``"unique"``    — exactly one safe match (adopt it).
      * ``"none"``      — no match at all (confirmed-absent; feeds the abandon
                          quorum).
      * ``"ambiguous"`` — >1 candidate matches; refuse to guess (leave the
                          intent unresolved + page the operator).

    Strict match (tightened 2026-06-20): exact symbol, action == "BUY" (a
    missing/blank action no longer matches), exact integer quantity, and a
    limit_price that is PRESENT and equal to the intent's (a missing limit is no
    longer a wildcard). Origin guard: the order id must not be ``claimed`` by an
    existing paper-auto ledger NOR belong to the human-discretionary track —
    the sweep can only adopt an order the bot itself could have placed.
    """
    human_ids = human_ids or set()
    want_sym = rec.ticker.upper()
    want_qty = int(rec.shares)
    want_limit = round(float(rec.limit_price), 2)
    all_orders = list(open_orders) + list(filled_orders)

    # FIX 4 — strongest origin proof: an EXACT client-order-tag (user_mark) echo
    # of this intent's cloid. The bot stamped it at placement, so even a lost
    # order id is recoverable AND a human order can never collide. If the broker
    # echoes the tag we trust it outright (still excluding claimed ids).
    tag_matches = [
        int(o["order_id"]) for o in all_orders
        if o.get("order_id") is not None
        and str(o.get("user_mark") or "") == rec.client_order_id
        and int(o["order_id"]) not in claimed
    ]
    if len(tag_matches) == 1:
        return tag_matches[0], "unique"
    if len(tag_matches) > 1:
        return None, "ambiguous"

    matches: list[int] = []
    for o in all_orders:
        oid = o.get("order_id")
        if oid is None:
            continue
        oid = int(oid)
        if oid in claimed or oid in human_ids:
            continue                                   # origin guard
        if str(o.get("symbol", "")).upper() != want_sym:
            continue
        if str(o.get("action", "")).upper() != "BUY":  # exact BUY, no "" allowance
            continue
        if int(o.get("quantity") or 0) != want_qty:
            continue
        lim = o.get("limit_price")
        if lim is None or round(float(lim), 2) != want_limit:  # limit required + exact
            continue
        if oid not in matches:
            matches.append(oid)
    if len(matches) == 1:
        return matches[0], "unique"
    if len(matches) == 0:
        return None, "none"
    return None, "ambiguous"


def reconcile_intents(
    *,
    client: TigerClient | None = None,
    open_orders: list[dict[str, Any]] | None = None,
    filled_orders: list[dict[str, Any]] | None = None,
    holdings: dict[str, float] | None = None,
    dry_run: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    account_alive: bool | None = None,
    today: _dt.date | None = None,
    holdings_avg_cost: dict[str, float] | None = None,
) -> list[IntentReconcileResult]:
    """Drive every non-terminal write-ahead intent to a terminal state.

    This is the recovery half of the write-ahead protocol in
    :func:`tools.auto_paper.pipeline.place_candidate`. For each intent that
    never reached ``ledgered`` (a crash / exception between place and
    ledger-write):

      * **ledger already exists** for the ticker → the ledger write actually
        succeeded; only the final intent flip was lost → mark ``ledgered``.
      * **status=placed, no ledger** → the order is live at the broker but the
        ledger never got written → reconstruct the submitted ledger from the
        intent (carrying its broker_order_id) → mark ``ledgered``. The normal
        ``reconcile_today`` flow then resolves the fill / expiry by order id,
        so a legitimate no-fill lands cleanly in ``closed_unfilled`` rather
        than as a black hole.
      * **status=intent, no ledger** → we never confirmed the order reached the
        broker. Match it against live open/filled orders; if found, adopt it
        (reconstruct ledger); if not, the order never placed → mark
        ``abandoned`` (clean terminal, nothing orphaned).

    Idempotent + re-run safe: terminal intents are skipped; a reconstructed
    ledger is guarded by ``state.ledger_exists``; matched orders are checked
    against the set already claimed by existing ledgers.

    Args:
        client: paper-routed TigerClient (constructed if None and a broker
            fetch is needed).
        open_orders / filled_orders / holdings: test seams — when provided, no
            broker round-trip is made.
        dry_run: classify + report without writing ledgers / positions.json or
            mutating intents.
        lookback_days: filled-order lookback window.

    Returns:
        list[IntentReconcileResult], one per unresolved intent (empty if none).
    """
    intents = intent_log.unresolved_intents()
    if not intents:
        return []

    # Gather broker views unless injected. `c` is always defined (= client when
    # views are injected) so the held-recovery stop placement + account-liveness
    # corroboration can use it.
    c = client
    need_broker = open_orders is None or filled_orders is None or holdings is None
    if need_broker:
        try:
            c = client or TigerClient()
        except BrokerConfigError as exc:
            return [
                IntentReconcileResult(
                    client_order_id=r.client_order_id, ticker=r.ticker,
                    action="error", broker_order_id=r.broker_order_id,
                    reason=f"broker config: {exc}",
                ) for r in intents
            ]
        start = (_dt.date.today() - _dt.timedelta(days=lookback_days)).isoformat()
        end = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
        try:
            if open_orders is None:
                open_orders = c.open_orders().output.get("orders", [])
            if filled_orders is None:
                filled_orders = c.get_filled_orders(start_time=start, end_time=end).output.get("orders", [])
            if holdings is None:
                positions = c.positions().output["positions"]
                holdings = {p["symbol"].upper(): float(p["quantity"]) for p in positions}
                if holdings_avg_cost is None:
                    holdings_avg_cost = {
                        p["symbol"].upper(): float(p.get("average_cost") or 0.0)
                        for p in positions
                    }
        except BrokerOrderError as exc:
            return [
                IntentReconcileResult(
                    client_order_id=r.client_order_id, ticker=r.ticker,
                    action="error", broker_order_id=r.broker_order_id,
                    reason=f"broker fetch: {exc}",
                ) for r in intents
            ]

    open_orders = open_orders or []
    filled_orders = filled_orders or []
    holdings = holdings or {}
    open_by_id = {o.get("order_id"): o for o in open_orders if o.get("order_id") is not None}
    filled_by_id = {o.get("order_id"): o for o in filled_orders if o.get("order_id") is not None}
    held = {t.upper() for t, q in holdings.items() if q and float(q) >= 1}  # LONGS only
    claimed = _claimed_order_ids()
    human_ids = _human_track_order_ids()      # origin deny-set (FIX 2)

    # Broker-trust signals for the abandon decision (FIX 2/3).
    #   broker_showing_data: the broker returned REAL order/position data this
    #     sweep → proof it is not blacked out → RESET the consecutive empty tally.
    #   broker_alive: account proven live (account_summary non-zero) OR data shown
    #     → a genuinely-empty live account is abandonable; a total blackout is NOT.
    broker_showing_data = bool(open_orders or filled_orders or holdings)
    if account_alive is None:
        broker_alive = broker_showing_data or _account_alive(c)
    else:
        broker_alive = bool(account_alive) or broker_showing_data
    today_str = (today or _dt.date.today()).isoformat()

    results: list[IntentReconcileResult] = []
    for rec in intents:
        cloid = rec.client_order_id
        # 1. Ledger already exists → the write succeeded; only the flip was lost.
        if state.ledger_exists(rec.ticker):
            if not dry_run:
                intent_log.mark(cloid, intent_log.STATUS_LEDGERED,
                                note="ledger already present; intent flip recovered")
            results.append(IntentReconcileResult(
                cloid, rec.ticker, "ledgered_already",
                broker_order_id=rec.broker_order_id,
                reason="canonical ledger already on disk; intent advanced to terminal",
            ))
            continue

        # 2. status=placed → broker has the order; rebuild the missing ledger.
        if rec.status == intent_log.STATUS_PLACED and rec.broker_order_id is not None:
            oid = int(rec.broker_order_id)
            if dry_run:
                results.append(IntentReconcileResult(
                    cloid, rec.ticker, "dry_run", broker_order_id=oid,
                    reason="would reconstruct ledger from placed intent",
                ))
                continue
            try:
                _reconstruct_ledger_from_intent(rec, oid)
            except state.PaperAutoStateError as exc:
                results.append(IntentReconcileResult(
                    cloid, rec.ticker, "error", broker_order_id=oid,
                    reason=f"ledger reconstruct failed: {exc}",
                ))
                continue
            intent_log.mark(cloid, intent_log.STATUS_LEDGERED,
                            note="ledger reconstructed from placed intent (orphan recovery)")
            present = oid in open_by_id or oid in filled_by_id or rec.ticker.upper() in held
            results.append(IntentReconcileResult(
                cloid, rec.ticker, "ledgered_recovered", broker_order_id=oid,
                reason=(
                    "reconstructed submitted ledger from placed intent; "
                    + ("order confirmed live at broker" if present
                       else "order not in current open/filled window — "
                            "reconcile_today will resolve to fill or closed_unfilled")
                ),
            ))
            continue

        # 3. status=intent (id unknown) → confirm the order actually placed.
        # Reaching here means the broker fetch was POSITIVELY successful (a soft
        # failure / None raised above and returned "error" for every intent), so
        # a "none" match is a *confirmed* absence — but we STILL never abandon on
        # one sweep (FIX 1b): a transient empty read must not torch a live order.
        matched, match_status = _match_open_or_filled(
            rec, open_orders=open_orders, filled_orders=filled_orders,
            claimed=claimed, human_ids=human_ids,
        )

        if match_status == "ambiguous":
            # >1 candidate matches — refuse to guess; leave unresolved + page.
            results.append(IntentReconcileResult(
                cloid, rec.ticker, "unresolved", broker_order_id=None,
                reason=("multiple broker orders match (symbol/qty/limit) — refusing "
                        "to auto-adopt; operator must reconcile manually"),
            ))
            continue

        if match_status == "unique":
            if dry_run:
                results.append(IntentReconcileResult(
                    cloid, rec.ticker, "dry_run", broker_order_id=matched,
                    reason="would adopt matched broker order + reconstruct ledger",
                ))
                continue
            try:
                _reconstruct_ledger_from_intent(rec, matched)
            except state.PaperAutoStateError as exc:
                results.append(IntentReconcileResult(
                    cloid, rec.ticker, "error", broker_order_id=matched,
                    reason=f"ledger reconstruct failed: {exc}",
                ))
                continue
            claimed.add(matched)
            intent_log.mark(cloid, intent_log.STATUS_PLACED, broker_order_id=matched)
            intent_log.mark(cloid, intent_log.STATUS_LEDGERED,
                            note="adopted matched broker order (orphan recovery)")
            results.append(IntentReconcileResult(
                cloid, rec.ticker, "ledgered_recovered", broker_order_id=matched,
                reason="uniquely matched a live broker order to the intent; reconstructed",
            ))
            continue

        # match_status == "none": no matching order THIS sweep.
        # FIX 1 — held-recovery: if the broker HOLDS the symbol, the order filled
        # (id lost). NEVER abandon. Reconstruct into STARTER (visible to
        # refresh_starter_stops / _starter_positions / classify_holdings) AND
        # place a protective STP inline so the held shares are never left
        # unstopped + invisible.
        # Long-only invariant: if the broker is SHORT this intent's symbol, do NOT
        # reconstruct into starter + STP — that arms a SELL that deepens the short.
        # Gate + surface; the operator flattens. (`held` is longs-only, so a short
        # is not in it and would otherwise fall through to the abandon logic.)
        signed_q = float(holdings.get(rec.ticker.upper(), 0) or 0)
        if signed_q <= -1:
            if not dry_run:
                cron_gate.set_gate(
                    reason="short_anomaly",
                    payload={"short_anomaly": [rec.ticker.upper()],
                             "quantities": {rec.ticker.upper(): signed_q},
                             "source": "intent_recovery"},
                )
            results.append(IntentReconcileResult(
                cloid, rec.ticker, "short_anomaly", broker_order_id=rec.broker_order_id,
                reason=("broker is SHORT this symbol (long-only invariant breach); NOT "
                        "reconstructed. Gated; operator must flatten (BUY to close)."),
            ))
            continue
        if rec.ticker.upper() in held:
            held_qty = int(float(holdings.get(rec.ticker.upper(), 0) or 0)) or int(rec.shares)
            # FOLD-IN #7 — book the broker's ACTUAL average cost as the entry, not
            # the limit-price placeholder (a gap-through recovery otherwise writes a
            # wrong R-multiple / realized P&L into the calibration log that gates
            # the flip-to-live sizing decision). Stop math uses stop_price, unaffected.
            avg_cost = float((holdings_avg_cost or {}).get(rec.ticker.upper()) or 0.0)
            if avg_cost <= 0:
                avg_cost = float(rec.limit_price)
            if dry_run:
                results.append(IntentReconcileResult(
                    cloid, rec.ticker, "dry_run",
                    reason="held at broker; would reconstruct into starter + place stop"))
                continue
            try:
                _reconstruct_ledger_from_intent(rec, None)
                # submitted -> starter, booking the real average cost as fill_price.
                _update_ledger_filled(rec.ticker, avg_fill_price=avg_cost,
                                      filled_qty=held_qty, requested_qty=int(rec.shares))
                _update_positions_json_filled(rec.ticker, avg_fill_price=avg_cost,
                                              filled_qty=held_qty, requested_qty=int(rec.shares))
            except (state.PaperAutoStateError, Exception) as exc:  # noqa: BLE001
                results.append(IntentReconcileResult(
                    cloid, rec.ticker, "error",
                    reason=f"held-recovery reconstruct failed: {exc!r}"))
                continue
            stop_sid, stop_err = (None, "no client available to place stop")
            if c is not None:
                stop_sid, stop_err = _ensure_stop_for(
                    rec.ticker, held_qty, open_by_id=open_by_id, client=c)
            # FOLD-IN #1 — if the stop did NOT get placed, the held starter is
            # NAKED. Record the error on the ledger so the health check surfaces it
            # on the pageable surface (a starter the system believes is protected
            # but isn't). refresh_starter_stops re-arms it in the common case; this
            # catches the persistently un-armable one.
            if stop_sid is None:
                _record_stop_place_error(rec.ticker, stop_err or "stop not placed")
            intent_log.mark(cloid, intent_log.STATUS_LEDGERED,
                            note=(f"broker holds {held_qty} @ avg {avg_cost:.4f}; reconstructed "
                                  f"into starter + stop={stop_sid} ({stop_err or 'ok'})"))
            results.append(IntentReconcileResult(
                cloid, rec.ticker, "ledgered_recovered", broker_order_id=stop_sid,
                reason=(f"broker holds the symbol with no ledger; reconstructed into "
                        f"starter @ avg {avg_cost:.4f} + protective stop "
                        f"({'placed #' + str(stop_sid) if stop_sid else 'FAILED: ' + str(stop_err)})")))
            continue

        # Not held, no match (absent from open + filled + holdings + user_mark
        # tag echo — all guaranteed by reaching here).
        #
        # FIX 6 — PER-INTENT evidence gate (NOT whole-book emptiness): the v3 gate
        # reset the tally whenever the broker held ANY unrelated position, so a
        # never-placed order could never reach quorum in a live book (dangled +
        # paged forever, permanently blocking its ticker). The tally now advances
        # on a CORROBORATED genuine absence — broker provably alive AND THIS order
        # genuinely nowhere — and resets ONLY on a blackout, never on unrelated
        # book data (unrelated data is itself proof the broker is live while our
        # order is absent → it corroborates, it does not reset).
        #
        # FIX 2 — a blackout (NOT broker_alive) never advances + resets the chain.
        #
        # RESIDUAL (documented, cannot be made 100% safe): a PERSISTENT soft-empty
        # on the open-orders endpoint specifically — while account_summary +
        # holdings stay healthy — hides a live RESTING order and is indistinguishable
        # from a genuine absence; it would abandon after ABANDON_QUORUM distinct
        # days. Bounded by: (a) broker_alive gate so a whole-API outage can't
        # abandon; (b) absence required across open AND filled AND holdings AND the
        # user_mark tag echo; (c) >= 2 distinct UTC dates + skip-creation-day. The
        # page-until-resolved path is the safe fallback while corroboration runs.
        if not broker_alive:
            if not dry_run and int(rec.empty_sweep_count) != 0:
                intent_log.update_intent(
                    cloid, empty_sweep_count=0,
                    note="broker blackout (account not provably live) — reset empty-sweep tally")
            results.append(IntentReconcileResult(
                cloid, rec.ticker, "unresolved", broker_order_id=None,
                reason="broker blackout (account not provably live) — NOT abandoned; "
                       "tally reset, kept unresolved + paging until the broker is verified"))
            continue

        # Broker PROVEN alive + THIS order genuinely absent → corroborated absence.
        # Count toward the quorum — ONCE PER DAY (distinct UTC date), never on the
        # intent's creation day (min wall-clock age before any abandon).
        created_date = (rec.created_at or "")[:10]
        already_today = (rec.last_empty_sweep_date == today_str)
        too_fresh = (today_str == created_date)
        if dry_run:
            results.append(IntentReconcileResult(
                cloid, rec.ticker, "dry_run",
                reason=f"clean-empty live account; sweep {rec.empty_sweep_count + (0 if (already_today or too_fresh) else 1)}/{ABANDON_QUORUM}"))
            continue
        if already_today or too_fresh:
            results.append(IntentReconcileResult(
                cloid, rec.ticker, "unresolved", broker_order_id=None,
                reason=("already counted today" if already_today
                        else "intent too fresh (created today) — min-age guard before abandon")))
            continue
        new_count = int(rec.empty_sweep_count) + 1
        if new_count < ABANDON_QUORUM:
            intent_log.update_intent(
                cloid, empty_sweep_count=new_count, last_empty_sweep_date=today_str,
                note=f"clean-empty live account, no order (sweep {new_count}/{ABANDON_QUORUM} "
                     f"on distinct dates); kept unresolved — quorum not met")
            results.append(IntentReconcileResult(
                cloid, rec.ticker, "unresolved", broker_order_id=None,
                reason=(f"clean-empty live account on sweep {new_count}/{ABANDON_QUORUM} "
                        f"(distinct dates); NOT abandoned yet")))
            continue
        intent_log.update_intent(
            cloid, status=intent_log.STATUS_ABANDONED, empty_sweep_count=new_count,
            last_empty_sweep_date=today_str,
            note=f"clean-empty live account across {new_count} distinct-date sweeps; "
                 f"order never reached the broker")
        results.append(IntentReconcileResult(
            cloid, rec.ticker, "abandoned",
            reason=(f"clean-empty live account across {new_count} distinct-date sweeps "
                    f"(quorum {ABANDON_QUORUM}); intent never placed (clean terminal)")))

    return results


def reconcile_today(
    *,
    client: TigerClient | None = None,
    dry_run: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[ReconcileResult]:
    """Reconcile all pending paper-auto positions against Tiger's fill history.

    Args:
        client: an existing :class:`TigerClient`. When None, constructs a
            paper-routed client.
        dry_run: when True, computes what would change without writing
            ledgers / positions.json AND without placing the broker-side
            protective stop.
        lookback_days: how many days back to pull filled orders. Default 5
            covers a long weekend.

    Returns:
        list[ReconcileResult] — one per pending paper-auto position.
        Empty list if nothing pending.
    """
    # Recover any dangling write-ahead intents FIRST (a crash-orphaned order
    # gets its ledger reconstructed here) so the pending-submitted loop below
    # then reconciles its fill / expiry in the same pass. Run for side effects;
    # the intent verdicts are reported separately by reconcile_intents callers.
    try:
        reconcile_intents(client=client, dry_run=dry_run, lookback_days=lookback_days)
    except Exception:  # noqa: BLE001 — recovery is best-effort; never block reconcile
        pass

    pending = _pending_submitted_ledgers()
    # Reconcile must also run when there are only starter / pending_close
    # positions (no new submissions) — otherwise stop-outs + pending_close
    # fills + self-healing stop refresh are silently skipped on quiet days.
    affected = pending + _pending_close_ledgers() + _starter_positions()
    if not affected:
        return []

    try:
        c = client or TigerClient()
    except BrokerConfigError as exc:
        return [
            ReconcileResult(
                ticker=p.get("ticker", "UNKNOWN"),
                action="error",
                reason=f"broker config: {exc}",
            ) for p in affected
        ]

    start = (_dt.date.today() - _dt.timedelta(days=lookback_days)).isoformat()
    end = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()

    try:
        filled_entry = c.get_filled_orders(start_time=start, end_time=end)
        open_entry = c.open_orders()
        # Holdings — for the FIX 4 expiry guard: never expire a submitted order
        # to closed_unfilled if the broker actually HOLDS the symbol (the fill
        # may have landed outside the lookback). Fetched in the same try so a
        # broker failure fails safe (error for all) rather than wrongly expiring.
        positions_entry = c.positions()
    except BrokerOrderError as exc:
        return [
            ReconcileResult(
                ticker=p.get("ticker", "UNKNOWN"),
                action="error",
                reason=f"broker fetch: {exc}",
            ) for p in affected
        ]

    filled_by_id = {
        o.get("order_id"): o for o in filled_entry.output.get("orders", [])
        if o.get("order_id") is not None
    }
    open_by_id = {
        o.get("order_id"): o for o in open_entry.output.get("orders", [])
        if o.get("order_id") is not None
    }
    held_symbols = {
        str(p.get("symbol", "")).upper()
        for p in positions_entry.output.get("positions", [])
        # LONGS only (signed >= 1). A SHORT must not read as "held" — else a
        # submitted order on a short symbol parks in `held_no_expire` limbo
        # forever (long-only invariant; consistent with the sign-aware fix).
        if p.get("quantity") and float(p.get("quantity") or 0) >= 1
    }

    results: list[ReconcileResult] = []
    # Stop-out detection MUST run BEFORE the refresh: a position whose
    # protective STP filled has its stop_order_id in the FILLED list (not
    # open_orders), so refresh would otherwise see "no live stop" and re-arm a
    # STP on a position we no longer hold. Closing it here removes it from the
    # starter set the refresh re-reads.
    results.extend(reconcile_stop_outs(
        client=c, filled_by_id=filled_by_id, dry_run=dry_run,
    ))
    # Self-healing stop refresh — run BEFORE processing submitted fills so
    # the post-fill stop-placement path stays unchanged. See
    # :func:`refresh_starter_stops` docstring for the recurrence-prevention
    # rationale (Tiger paper STPs are DAY-only).
    results.extend(refresh_starter_stops(
        client=c, open_by_id=open_by_id, dry_run=dry_run,
    ))

    for entry in pending:
        ticker = entry["ticker"]
        order_id = entry["broker_order_id"]
        requested_qty = int(entry.get("shares") or 0)

        filled = filled_by_id.get(order_id)
        if filled is not None:
            filled_qty = int(filled.get("filled_quantity") or 0)
            avg_fill = filled.get("avg_fill_price")
            if filled_qty <= 0 or avg_fill is None:
                results.append(ReconcileResult(
                    ticker=ticker, action="error",
                    broker_order_id=order_id,
                    reason=f"order in filled list but missing qty/avg_fill: {filled}",
                ))
                continue

            action = "filled" if filled_qty == requested_qty else "partial"
            stop_order_id: int | None = None
            stop_err: str | None = None
            if not dry_run:
                try:
                    _update_ledger_filled(
                        ticker,
                        avg_fill_price=float(avg_fill),
                        filled_qty=filled_qty,
                        requested_qty=requested_qty,
                    )
                    _update_positions_json_filled(
                        ticker,
                        avg_fill_price=float(avg_fill),
                        filled_qty=filled_qty,
                        requested_qty=requested_qty,
                    )
                except state.PaperAutoStateError as exc:
                    results.append(ReconcileResult(
                        ticker=ticker, action="error",
                        broker_order_id=order_id,
                        reason=f"ledger update: {exc}",
                    ))
                    continue

                # Session 3 — place the broker-side protective stop sized
                # to the ACTUAL filled quantity (handles partial fills).
                stop_order_id, stop_err = _place_broker_stop_on_fill(
                    client=c, ticker=ticker, filled_qty=filled_qty,
                )

            results.append(ReconcileResult(
                ticker=ticker, action=action,
                broker_order_id=order_id,
                requested_qty=requested_qty,
                filled_qty=filled_qty,
                avg_fill_price=float(avg_fill),
                stop_order_id=stop_order_id,
                stop_place_error=stop_err,
            ))
            continue

        if order_id in open_by_id:
            # Order still open at broker (rare for DAY orders — pre-market
            # submits, or extended-hours TIF). No state change.
            results.append(ReconcileResult(
                ticker=ticker, action="still_open",
                broker_order_id=order_id,
                requested_qty=requested_qty,
                reason="order still open at broker; no state change",
            ))
            continue

        # FIX 4: never expire a symbol the broker actually HOLDS — the fill may
        # have landed outside the lookback window (common for intent-reconstructed
        # ledgers). Leave it submitted; a later reconcile with a fresh fill window
        # (or the stuck-closing reconciler) resolves it.
        if ticker.upper() in held_symbols:
            results.append(ReconcileResult(
                ticker=ticker, action="held_no_expire",
                broker_order_id=order_id, requested_qty=requested_qty,
                reason=("order not in fill/open window BUT broker holds the symbol — "
                        "not expiring (fill likely outside lookback); left submitted"),
            ))
            continue

        # Not filled today, not open now → DAY-expired (or cancelled outside framework).
        reason = "TIF=DAY expired unfilled (or cancelled outside framework)"
        if not dry_run:
            try:
                _update_ledger_expired(ticker, reason)
                _update_positions_json_expired(ticker)
            except state.PaperAutoStateError as exc:
                results.append(ReconcileResult(
                    ticker=ticker, action="error",
                    broker_order_id=order_id,
                    reason=f"ledger update: {exc}",
                ))
                continue
        results.append(ReconcileResult(
            ticker=ticker, action="expired",
            broker_order_id=order_id,
            requested_qty=requested_qty,
            reason=reason,
        ))

    # --- pending_close processing (added 2026-06-02, paired with the
    # exits.py Bug-1 fix) -------------------------------------------------
    # For each ledger in pending_close, look up its pending_sell_order_id.
    #   filled  -> meta.state := closed; cancel any resting stop;
    #              remove from positions.json
    #   expired -> meta.state := starter; clear pending_sell_order_id;
    #              leave protective stop alone (it was never cancelled)
    #   open    -> no change (rare for DAY orders)
    pending_close = _pending_close_ledgers()
    for entry in pending_close:
        ticker = entry["ticker"]
        sell_oid = entry.get("pending_sell_order_id")
        if sell_oid is None:
            results.append(ReconcileResult(
                ticker=ticker, action="error",
                reason="pending_close entry missing pending_sell_order_id",
            ))
            continue

        filled = filled_by_id.get(sell_oid)
        if filled is not None:
            filled_qty = int(filled.get("filled_quantity") or 0)
            avg_fill = filled.get("avg_fill_price")
            if filled_qty <= 0 or avg_fill is None:
                results.append(ReconcileResult(
                    ticker=ticker, action="error",
                    broker_order_id=sell_oid,
                    reason=f"sell in filled list but missing qty/avg_fill: {filled}",
                ))
                continue

            # Cancel the resting protective stop (which exits.py intentionally
            # left in place until fill confirmation).
            stop_oid = _existing_stop_order_id(ticker)
            stop_cancel_err: str | None = None
            if stop_oid is not None and not dry_run:
                try:
                    cancel_entry = c.cancel(order_id=stop_oid)
                    if not cancel_entry.output.get("accepted"):
                        stop_cancel_err = f"broker did not accept cancel of stop #{stop_oid}"
                except BrokerOrderError as exc:
                    stop_cancel_err = f"cancel(stop #{stop_oid}): {exc}"

            if not dry_run:
                try:
                    _update_ledger_closed_from_pending(
                        ticker,
                        exit_price=float(avg_fill),
                        exit_reason=f"exit_fill from order #{sell_oid}",
                    )
                    _update_positions_json_closed_from_pending(ticker)
                except state.PaperAutoStateError as exc:
                    results.append(ReconcileResult(
                        ticker=ticker, action="error",
                        broker_order_id=sell_oid,
                        reason=f"ledger close-from-pending: {exc}",
                    ))
                    continue

            results.append(ReconcileResult(
                ticker=ticker,
                action="exit_filled",
                broker_order_id=sell_oid,
                requested_qty=int(entry.get("shares") or 0),
                filled_qty=filled_qty,
                avg_fill_price=float(avg_fill),
                stop_order_id=stop_oid,
                stop_place_error=stop_cancel_err,
            ))
            continue

        if sell_oid in open_by_id:
            results.append(ReconcileResult(
                ticker=ticker, action="exit_still_open",
                broker_order_id=sell_oid,
                requested_qty=int(entry.get("shares") or 0),
                reason="exit limit-sell still open at broker; no state change",
            ))
            continue

        # Sell order is gone from broker but not in today's fills =
        # DAY-expired unfilled (or manually cancelled). Revert to starter;
        # the protective stop is still in place because exits.py never
        # cancelled it.
        revert_reason = (
            f"exit limit-sell #{sell_oid} expired unfilled "
            "(or cancelled outside framework); position reverts to starter"
        )
        if not dry_run:
            try:
                _revert_ledger_to_starter_from_pending(ticker, revert_reason)
                _revert_positions_json_to_starter_from_pending(ticker)
            except state.PaperAutoStateError as exc:
                results.append(ReconcileResult(
                    ticker=ticker, action="error",
                    broker_order_id=sell_oid,
                    reason=f"ledger revert-to-starter: {exc}",
                ))
                continue

        results.append(ReconcileResult(
            ticker=ticker,
            action="exit_expired_reverted",
            broker_order_id=sell_oid,
            requested_qty=int(entry.get("shares") or 0),
            reason=revert_reason,
        ))

    return results
