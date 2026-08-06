"""Auction-order (LOO) capability test — Tiger PAPER account. Operator-authorized 2026-08-06.

Verifies the doctrine review's §3.2 open item: does the paper account ACCEPT
`auction_limit_order` (limit-on-open)? Designed for zero side effects:
  - PAPER-routed (TigerClient refuses live by construction; never overridden here).
  - BUY 1 share with limit ~20% BELOW market -> the opening auction can never fill it.
  - Cancelled immediately after acceptance regardless.
The test result is API ACCEPTANCE (or the verbatim rejection), not execution.
Throwaway probe per convention; production TigerClient is NOT modified.

Usage:  uv run python ledgers/improvements/2026-08-06-auction-order-capability-test.py
"""
from __future__ import annotations
import os, sys, time
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from tools.broker.tiger import TigerClient

SYMBOL = "F"          # cheap, ultra-liquid — minimizes any residual notional risk
QTY = 1


def main():
    client = TigerClient()                      # paper-routed; raises if live
    print("client constructed (paper-routed by construction)", flush=True)

    # Reference price from cached daily data — the live quote path needs a US
    # quote entitlement this device currently lacks (verified 2026-08-06:
    # code=4 permission denied on get_briefs). Trading permission is a separate
    # domain; the order attempt below is the actual test.
    from tools.data import fetch_ohlcv
    df = fetch_ohlcv(SYMBOL, period="5d", interval="1d").df
    latest = float(df["Close"].iloc[-1]) if df is not None and len(df) else 0.0
    if latest <= 0:
        print("no quote — abort"); return
    limit = round(latest * 0.80, 2)             # unmarketable-low: auction cannot fill it
    print(f"{SYMBOL} latest={latest} -> test LOO BUY {QTY} @ {limit} (unmarketable)", flush=True)

    from tigeropen.common.util.order_utils import auction_limit_order

    contract = client._tc.get_contract(symbol=SYMBOL)
    order = auction_limit_order(
        account=client._account, contract=contract, action="BUY",
        quantity=QTY, limit_price=limit,
    )
    print(f"order object: type={getattr(order, 'order_type', None)} tif={getattr(order, 'time_in_force', None)}", flush=True)

    try:
        order_id = client._tc.place_order(order)
        print(f"PLACE RESULT: ACCEPTED — order_id={order_id}", flush=True)
    except Exception as exc:  # noqa: BLE001 — verbatim rejection IS the data
        print(f"PLACE RESULT: REJECTED — {exc!r}", flush=True)
        return

    time.sleep(2)
    # read back status, then cancel regardless
    try:
        oo = client.open_orders()
        rows_raw = oo.output if isinstance(oo.output, (list, tuple)) else (oo.output or {}).get("orders", [])
        rows = [o for o in rows_raw
                if str((o.get("order_id") if isinstance(o, dict) else getattr(o, "id", None)) or "") == str(order_id)
                or str((o.get("id") if isinstance(o, dict) else "") or "") == str(order_id)]
        print(f"read-back: {rows if rows else 'not matched in open_orders (may be held/queued at broker)'}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"read-back failed (non-fatal): {exc!r}", flush=True)
    try:
        res = client.cancel(order_id)
        print(f"CANCEL RESULT: {res.output}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"CANCEL RESULT: FAILED — {exc!r} (order is unmarketable-low; expires at day end)", flush=True)


if __name__ == "__main__":
    main()
