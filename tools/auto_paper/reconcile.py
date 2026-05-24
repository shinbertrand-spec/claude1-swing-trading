"""EOD reconciliation for the paper-auto track.

After the close, this module:

1. Loads all paper-auto positions still in the ``submitted`` state
2. Pulls today's filled + open orders from Tiger
3. For each pending ledger, matches against the broker by
   ``broker_order_id`` and decides:
     * **filled** — order filled completely → state ``submitted`` →
       ``starter``; ``fill_price`` ← broker ``avg_fill_price``
     * **partial** — partial fill (broker filled < requested) → state
       ``submitted`` → ``starter``; ``shares`` shrinks to ``filled_qty``;
       ``fill_price`` ← ``avg_fill_price``
     * **expired** — order not filled today and no longer open (TIF=DAY
       expired at close) → state ``submitted`` → ``closed`` with a note
     * **still_open** — order still open at the broker (rare for DAY but
       happens with pre-market submits) → no state change
     * **no_match** — no matching order at Tiger at all (e.g. cancelled
       manually outside the framework) → no change; flagged in the
       result
4. Updates the per-ticker ledger + ``journal/paper-auto/positions.json``

Session 2 scope: ``submitted`` → ``starter`` / ``closed`` transitions.
Broker-side stops + per-bar sell-decision exits are Session 3.
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
            ledgers / positions.json.
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
            results.append(ReconcileResult(
                ticker=ticker, action=action,
                broker_order_id=order_id,
                requested_qty=requested_qty,
                filled_qty=filled_qty,
                avg_fill_price=float(avg_fill),
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
