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
import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Optional

import yaml

from ..broker.tiger import BrokerConfigError, BrokerOrderError, TigerClient
from . import critic_panel, cron_gate, orphan_check, state

DEFAULT_LOOKBACK_DAYS = 5


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
        held = int(abs(holdings.get(ticker.upper(), 0)))
        if held < 1:
            results.append(ReconcileResult(
                ticker=ticker, action="not_held",
                reason=("ledger=starter but broker holds <1 share; STP SELL NOT "
                        "placed (would be naked short). Journal/broker desync — "
                        "stuck-closing / pre-session sweep reconciler's domain."),
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


def _persist_orphan_discovery(
    orphans: list[str],
    holdings: dict[str, float],
    *,
    source: str = "post_rth_reconciler",
    corrupt: Optional[list[str]] = None,
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

    # --- Mode A: stuck-closing flip-back ---
    for ticker in cls.stuck_closing:
        qty = int(abs(holdings[ticker]))
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
                filled_qty=int(abs(holdings[ticker])),
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
    if (cls.orphans or cls.corrupt_held) and not dry_run:
        discovery_path = _persist_orphan_discovery(
            cls.orphans, holdings,
            source="presession_sweep", corrupt=cls.corrupt_held,
        )
        cron_gate.set_gate(
            reason="presession_orphan_sweep",
            payload={
                "orphans": cls.orphans,
                "corrupt_held": cls.corrupt_held,
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
