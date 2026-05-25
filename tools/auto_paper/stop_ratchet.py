"""Trailing-stop ratchet for the paper-auto track.

Implements the CLAUDE.md trailing rules:

* ``Trail stops to breakeven once a position is +5%``
* ``Trail to +5% once at +10%``

Mechanic, for each ``starter``-state paper-auto position with an active
broker-side stop order:

1. Read current price (last bar's Close from OHLCV).
2. Compute ``gain_pct = (current_price - fill_price) / fill_price``.
3. Determine target stop per the two-tier ladder:

   * ``gain_pct >= 10%`` -> target = ``fill_price * 1.05``
   * ``gain_pct >= 5%``  -> target = ``fill_price`` (break-even)
   * otherwise           -> no ratchet candidate.

4. If ``target > current_stop`` (we only ever raise the stop, per
   CLAUDE.md), cancel the existing broker stop and place a new STP SELL
   for the same share count at the new price. Update
   ``position_state.current_stop`` + ``position_state.stop_order_id`` on
   the ledger.

If cancel succeeds but the subsequent place fails, the ledger records
``stop_order_id: null`` + a ``notes`` line — the position is temporarily
unprotected and the next ratchet pass (or the next reconcile) will
re-attempt. This is the safer failure mode than ``orphan stop +
unmatched ledger``.

Wired into ``/auto-paper-monitor`` after ``evaluate_exits()``: ratchet
fires for any starter position that wasn't just closed by the
sell-decision composer.

Idempotent: ``ratchet_all`` skips positions already at or above target.
``--dry-run`` writes nothing to the broker and nothing to the ledger.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Optional

import yaml

from ..broker.tiger import BrokerConfigError, BrokerOrderError, TigerClient
from ..data import fetch_ohlcv
from . import state

# Two-tier ratchet per CLAUDE.md § Risk Management. Edit these in lockstep
# with the doctrine; consider any change a behavior change.
RATCHET_TIER_1_PCT = 0.05  # at +5%, move stop to break-even
RATCHET_TIER_2_PCT = 0.10  # at +10%, move stop to +5%
RATCHET_TIER_2_STOP_PCT = 0.05  # +5% target stop in tier-2

# OHLCV window — only need recent bars for current price.
DEFAULT_OHLCV_PERIOD = "1mo"


@dataclass
class RatchetResult:
    """Per-position ratchet outcome from :func:`ratchet_all`."""
    ticker: str
    action: str  # "ratcheted" / "no_change" / "no_stop" / "error" / "dry_run"
    gain_pct: Optional[float] = None
    current_price: Optional[float] = None
    fill_price: Optional[float] = None
    old_stop: Optional[float] = None
    new_stop: Optional[float] = None
    old_stop_order_id: Optional[int] = None
    new_stop_order_id: Optional[int] = None
    shares: Optional[int] = None
    tier: Optional[int] = None  # 1 = breakeven, 2 = +5%
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _starter_positions() -> list[dict[str, Any]]:
    data = state.load_positions_json()
    return [
        p for p in data.get("positions", [])
        if p.get("stage") == "starter"
    ]


def _read_ledger(ticker: str) -> dict[str, Any] | None:
    p = state.ledger_path(ticker)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _ledger_stop_info(doc: dict[str, Any]) -> tuple[Optional[float], Optional[int]]:
    """Return (current_stop, stop_order_id) from the ledger.

    Prefers ``position_state.current_stop`` over
    ``setup_classification.stop_price`` (the immutable original).
    """
    ps = doc.get("position_state") or {}
    sc = doc.get("setup_classification") or {}
    current_stop = ps.get("current_stop")
    if not isinstance(current_stop, (int, float)):
        current_stop = sc.get("stop_price")
    stop_order_id = ps.get("stop_order_id")
    return (
        float(current_stop) if isinstance(current_stop, (int, float)) else None,
        int(stop_order_id) if isinstance(stop_order_id, (int, float)) else None,
    )


def _starter_fill_price(doc: dict[str, Any]) -> Optional[float]:
    ps = doc.get("position_state") or {}
    starter = ps.get("starter") or {}
    price = starter.get("fill_price")
    return float(price) if isinstance(price, (int, float)) else None


def _compute_target_stop(fill_price: float, current_price: float) -> tuple[Optional[float], Optional[int]]:
    """Return ``(target_stop, tier)`` or ``(None, None)`` if no ratchet yet."""
    gain = (current_price - fill_price) / fill_price
    if gain >= RATCHET_TIER_2_PCT:
        return round(fill_price * (1.0 + RATCHET_TIER_2_STOP_PCT), 2), 2
    if gain >= RATCHET_TIER_1_PCT:
        return round(fill_price, 2), 1
    return None, None


def _write_ledger(ticker: str, doc: dict[str, Any]) -> None:
    p = state.ledger_path(ticker)
    state._validate_against_schema(doc)  # noqa: SLF001 — intentional internal helper
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)


def _update_positions_json_stop(ticker: str, new_stop: float) -> None:
    if not os.path.isfile(state.PAPER_AUTO_POSITIONS_JSON):
        return
    with open(state.PAPER_AUTO_POSITIONS_JSON, encoding="utf-8") as fh:
        data = json.load(fh)
    for entry in data.get("positions", []):
        if entry.get("ticker") == ticker.upper():
            entry["stop"] = float(new_stop)
            break
    data["updated"] = _now_iso()
    with open(state.PAPER_AUTO_POSITIONS_JSON, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _ratchet_one(
    *,
    pos: dict[str, Any],
    client: TigerClient | None,
    dry_run: bool,
    fetch_ohlcv_fn,
) -> RatchetResult:
    ticker = pos["ticker"]
    doc = _read_ledger(ticker)
    if doc is None:
        return RatchetResult(ticker=ticker, action="error",
                             reason=f"ledger missing for {ticker}")

    fill_price = _starter_fill_price(doc)
    if fill_price is None:
        return RatchetResult(ticker=ticker, action="error",
                             reason=f"ledger missing starter fill_price for {ticker}")

    current_stop, old_stop_order_id = _ledger_stop_info(doc)
    if old_stop_order_id is None:
        # No active broker stop to ratchet. Reconcile will create one;
        # nothing for us to do here.
        return RatchetResult(
            ticker=ticker, action="no_stop",
            fill_price=fill_price, old_stop=current_stop,
            reason="no active broker stop (stop_order_id missing)",
        )

    try:
        ohlcv = fetch_ohlcv_fn(ticker, period=DEFAULT_OHLCV_PERIOD, interval="1d")
    except Exception as exc:  # noqa: BLE001
        return RatchetResult(ticker=ticker, action="error",
                             fill_price=fill_price,
                             reason=f"fetch_ohlcv failed: {exc}")
    df = ohlcv.df
    if df is None or df.empty:
        return RatchetResult(ticker=ticker, action="error",
                             fill_price=fill_price,
                             reason="fetch_ohlcv returned empty")
    current_price = float(df["Close"].iloc[-1])
    gain_pct = (current_price - fill_price) / fill_price

    target_stop, tier = _compute_target_stop(fill_price, current_price)
    if target_stop is None:
        return RatchetResult(
            ticker=ticker, action="no_change",
            fill_price=fill_price, current_price=current_price,
            gain_pct=gain_pct, old_stop=current_stop,
            reason=f"gain {gain_pct:+.2%} below tier-1 threshold ({RATCHET_TIER_1_PCT:.0%})",
        )

    if current_stop is not None and target_stop <= current_stop:
        return RatchetResult(
            ticker=ticker, action="no_change",
            fill_price=fill_price, current_price=current_price,
            gain_pct=gain_pct, old_stop=current_stop, new_stop=target_stop,
            tier=tier,
            reason=(f"current stop ${current_stop:.2f} already at/above "
                    f"tier-{tier} target ${target_stop:.2f}"),
        )

    shares = int(pos.get("shares") or 0)
    if shares <= 0:
        return RatchetResult(ticker=ticker, action="error",
                             reason=f"position has non-positive shares={shares}")

    if dry_run:
        return RatchetResult(
            ticker=ticker, action="dry_run",
            fill_price=fill_price, current_price=current_price,
            gain_pct=gain_pct, old_stop=current_stop, new_stop=target_stop,
            old_stop_order_id=old_stop_order_id, shares=shares, tier=tier,
            reason=(f"dry_run — would cancel stop #{old_stop_order_id} "
                    f"and place new STP SELL {shares} @ ${target_stop:.2f}"),
        )

    if client is None:
        return RatchetResult(ticker=ticker, action="error",
                             reason="no client passed and dry_run=False")

    # Cancel old stop. If broker says no, surface and bail without placing.
    try:
        cancel_entry = client.cancel(order_id=old_stop_order_id)
        if not cancel_entry.output.get("accepted"):
            return RatchetResult(
                ticker=ticker, action="error",
                fill_price=fill_price, current_price=current_price,
                gain_pct=gain_pct, old_stop=current_stop, new_stop=target_stop,
                old_stop_order_id=old_stop_order_id, shares=shares, tier=tier,
                reason=f"broker rejected cancel of stop #{old_stop_order_id}",
            )
    except BrokerOrderError as exc:
        return RatchetResult(
            ticker=ticker, action="error",
            fill_price=fill_price, current_price=current_price,
            gain_pct=gain_pct, old_stop=current_stop, new_stop=target_stop,
            old_stop_order_id=old_stop_order_id, shares=shares, tier=tier,
            reason=f"cancel(stop #{old_stop_order_id}): {exc}",
        )

    # Place new stop. If this fails, the position is temporarily
    # unprotected — clear stop_order_id on the ledger so the next
    # reconcile/ratchet pass re-attempts. Surface loudly.
    try:
        place_entry = client.place_stop_loss(
            symbol=ticker.upper(),
            quantity=shares,
            stop_price=target_stop,
        )
    except BrokerOrderError as exc:
        ps = doc.setdefault("position_state", {})
        ps.pop("stop_order_id", None)
        notes = doc.get("notes", "")
        warning = (f"stop_ratchet on {_today_iso()}: cancelled old stop "
                   f"#{old_stop_order_id} then place_stop_loss failed: {exc}. "
                   f"Position TEMPORARILY UNPROTECTED — next pass will retry.")
        doc["notes"] = f"{notes}\n{warning}".strip() if notes else warning
        try:
            _write_ledger(ticker, doc)
        except state.PaperAutoStateError:
            pass  # already reporting an error
        return RatchetResult(
            ticker=ticker, action="error",
            fill_price=fill_price, current_price=current_price,
            gain_pct=gain_pct, old_stop=current_stop, new_stop=target_stop,
            old_stop_order_id=old_stop_order_id, shares=shares, tier=tier,
            reason=f"OLD STOP CANCELLED but place_stop_loss FAILED: {exc}",
        )

    new_stop_order_id = place_entry.output.get("order_id")

    # Update ledger.
    ps = doc.setdefault("position_state", {})
    ps["current_stop"] = float(target_stop)
    if new_stop_order_id is not None:
        ps["stop_order_id"] = int(new_stop_order_id)
    notes = doc.get("notes", "")
    ratchet_note = (f"stop_ratchet on {_today_iso()}: tier-{tier} ratchet — "
                    f"old stop ${current_stop:.2f} -> ${target_stop:.2f} "
                    f"(gain {gain_pct:+.2%}); cancelled #{old_stop_order_id}, "
                    f"placed #{new_stop_order_id}")
    doc["notes"] = f"{notes}\n{ratchet_note}".strip() if notes else ratchet_note

    try:
        _write_ledger(ticker, doc)
    except state.PaperAutoStateError as exc:
        return RatchetResult(
            ticker=ticker, action="error",
            fill_price=fill_price, current_price=current_price,
            gain_pct=gain_pct, old_stop=current_stop, new_stop=target_stop,
            old_stop_order_id=old_stop_order_id,
            new_stop_order_id=int(new_stop_order_id) if new_stop_order_id else None,
            shares=shares, tier=tier,
            reason=(f"new stop #{new_stop_order_id} placed but ledger write failed: {exc}. "
                    "Manual reconciliation needed."),
        )

    _update_positions_json_stop(ticker, target_stop)

    return RatchetResult(
        ticker=ticker, action="ratcheted",
        fill_price=fill_price, current_price=current_price,
        gain_pct=gain_pct, old_stop=current_stop, new_stop=target_stop,
        old_stop_order_id=old_stop_order_id,
        new_stop_order_id=int(new_stop_order_id) if new_stop_order_id else None,
        shares=shares, tier=tier,
    )


def ratchet_all(
    *,
    client: TigerClient | None = None,
    dry_run: bool = False,
    fetch_ohlcv_fn=fetch_ohlcv,
) -> list[RatchetResult]:
    """Ratchet broker-side stops upward for every starter paper-auto position.

    Args:
        client: existing :class:`TigerClient`. When None and not dry-run,
            constructs a paper-routed client.
        dry_run: when True, computes the target and returns
            ``action="dry_run"`` without calling the broker or writing
            the ledger.
        fetch_ohlcv_fn: injectable for tests.

    Returns:
        list[RatchetResult] — one per starter-state paper-auto position.
        Empty when nothing is in the starter state.
    """
    starters = _starter_positions()
    if not starters:
        return []

    c: TigerClient | None = client
    if not dry_run and c is None:
        try:
            c = TigerClient()
        except BrokerConfigError as exc:
            return [
                RatchetResult(ticker=p["ticker"], action="error",
                              reason=f"broker config: {exc}")
                for p in starters
            ]

    return [
        _ratchet_one(pos=pos, client=c, dry_run=dry_run, fetch_ohlcv_fn=fetch_ohlcv_fn)
        for pos in starters
    ]
