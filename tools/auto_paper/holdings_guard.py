"""Point-of-sale long-only guard for the paper-auto monitor composers.

Second layer under the sign-aware reconciler (2026-07-24). The reconciler
hardened the code that WRITES the ledger, but the two monitor-side composers
that ACT on it — ``exits.evaluate_exits`` (limit-sells) and
``stop_ratchet.ratchet_all`` (STP re-arms) — size their orders from the ledger
``shares`` field, guarded only by ``shares <= 0``. A corrupt/stale POSITIVE
share count (e.g. the July NFLX incident: ledger ``starter`` 29,760 while the
broker was flat/short) would therefore place a 29,760-share SELL from a
flat/short book and open/deepen a short — the exact loop the reconciler fix
kills one layer up.

This module is the shared point-of-sale check both composers call before
placing: **never place a SELL/STP the broker can't back with a LONG position of
at least the requested size.**

Design note — fail-open on a broker READ failure, fail-closed on a successful
read that shows insufficient long. If the holdings snapshot is unavailable
(broker error), the guard cannot run and the caller proceeds with a logged
warning (no worse than pre-guard behaviour, and avoids hard-blocking exits on a
transient broker hiccup). When the snapshot IS available, an absent / flat /
short / under-held ticker is refused.
"""
from __future__ import annotations

from typing import Any


def fetch_signed_holdings(client: Any) -> dict[str, float]:
    """Return ``{TICKER: signed_qty}`` from the broker (shorts stay negative).

    Raises whatever ``client.positions()`` raises — the caller decides whether a
    fetch failure fails open. Never applies ``abs()``: sign is the whole point.
    """
    positions = client.positions().output["positions"]
    return {p["symbol"].upper(): float(p["quantity"]) for p in positions}


def long_backs_sell(
    holdings: dict[str, float], ticker: str, requested_shares: int | float
) -> bool:
    """True iff the broker holds at least ``requested_shares`` LONG of ``ticker``.

    A flat (absent / 0), short (< 0), or under-held position returns False — a
    SELL/STP of ``requested_shares`` against it would create or deepen a short.
    """
    return float(holdings.get(ticker.upper(), 0.0)) >= float(requested_shares)
