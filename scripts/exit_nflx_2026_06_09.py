"""Operator-initiated exit of the paper-auto NFLX position (2026-06-09).

Context: connors_rsi2 was PARKED 2026-06-09 (stale pre-concurrency-cap gate
verdict — re-run fails at Sharpe 0.59; commit 46356ed). NFLX was placed by
connors_rsi2 before parking AND its sell-discipline composer already fired
`sell_1_3` on 2026-06-05 (violations firing, base_stage 5). Decision: exit
NFLX, ride MO (MO is +4.3% with a breakeven stop — protected winner, kept).

This is a deliberate operator exit, not a composer auto-exit, so it does not
re-run the detectors. It mirrors the framework's pending_close pattern from
tools.auto_paper.exits (place limit-sell -> mark pending_close, LEAVE the
protective stop in place as backstop -> reconcile closes on confirmed fill).

Guards:
  * Refuses to place outside the regular US session (CLAUDE.md hard rule:
    never place trades when market status is closed). Run it at/after 09:30 ET.
  * Prices the limit off TIGER'S OWN MARK (market_value / quantity), NOT
    yfinance — yfinance mis-scales this paper symbol (~$1000 vs Tiger ~$82),
    which would place an unfillable / wrong limit.
  * Paper-only TigerClient (allow_live defaults False).

Usage:
  uv run python scripts/exit_nflx_2026_06_09.py            # place at the open
  uv run python scripts/exit_nflx_2026_06_09.py --dry-run  # show, don't place
  uv run python scripts/exit_nflx_2026_06_09.py --force     # bypass session guard (testing only)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.auto_paper import exits
from tools.broker.tiger import TigerClient

TICKER = "NFLX"
EXIT_REASON = "operator_exit_connors_rsi2_parked_2026_06_09"


def _session_open_now() -> bool:
    """True iff the US regular session is open (Mon-Fri, 09:30-16:00 ET)."""
    et = timezone(timedelta(hours=-4))  # EDT; close enough for the open guard
    now = datetime.now(timezone.utc).astimezone(et)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= mins < (16 * 60)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="bypass the market-open guard (testing only)")
    args = ap.parse_args()

    if not args.force and not _session_open_now():
        print("MARKET CLOSED -- refusing to place (CLAUDE.md hard rule). Run at/after 09:30 ET.")
        return 2

    client = TigerClient()  # paper-only
    pos = {p["symbol"]: p for p in client.positions().output["positions"]}
    p = pos.get(TICKER)
    if not p:
        print(f"{TICKER} NOT held at Tiger — nothing to exit (already flat / reconciled).")
        return 0

    qty = int(p["quantity"])
    mark = p["market_value"] / qty if qty else 0.0
    limit = round(mark * 0.999, 2)  # bid - 0.1% proxy off Tiger's own mark
    print(f"{TICKER}: qty={qty} avg_cost={p['average_cost']:.2f} mark~{mark:.2f} "
          f"uPnL={p['unrealized_pnl']:+,.0f} -> limit-sell @ {limit:.2f}")

    if args.dry_run:
        print("DRY-RUN -- no order placed.")
        return 0

    te = client.place_limit_sell(TICKER, qty, limit)
    order_id = te.output.get("order_id") if isinstance(te.output, dict) else te.output
    print(f"PLACED Tiger paper DAY limit-sell #{order_id} for {qty} {TICKER} @ {limit:.2f}")

    # Framework pending_close: leave the protective stop in place as backstop;
    # the reconciler completes the lifecycle (fill -> closed, expiry -> starter).
    doc = exits._read_ledger(TICKER)
    if doc is not None:
        exits._mark_ledger_pending_close(
            doc, pending_sell_order_id=order_id, sell_limit_price=limit, exit_reason=EXIT_REASON,
        )
        exits._write_ledger(TICKER, doc)
        exits._mark_positions_json_pending_close(TICKER, pending_sell_order_id=order_id)
        print(f"{TICKER} ledger + positions.json -> pending_close (stop left as backstop).")
    else:
        print(f"WARNING: {TICKER} ledger not found — order placed but state not transitioned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
