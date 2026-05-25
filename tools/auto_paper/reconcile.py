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
from . import state

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
    if not pending:
        return []

    try:
        c = client or TigerClient()
    except BrokerConfigError as exc:
        return [
            ReconcileResult(
                ticker=p.get("ticker", "UNKNOWN"),
                action="error",
                reason=f"broker config: {exc}",
            ) for p in pending
        ]

    start = (_dt.date.today() - _dt.timedelta(days=lookback_days)).isoformat()

    try:
        filled_entry = c.get_filled_orders(start_time=start)
        open_entry = c.open_orders()
    except BrokerOrderError as exc:
        return [
            ReconcileResult(
                ticker=p.get("ticker", "UNKNOWN"),
                action="error",
                reason=f"broker fetch: {exc}",
            ) for p in pending
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

    return results
