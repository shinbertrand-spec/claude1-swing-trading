"""Re-place the 3 paper-auto protective stops that DAY-expired at session close.

GO / VAL / MO are `starter` positions whose recorded stop_order_id points to an
order that expired (Tiger paper STP = DAY). Reconcile's stop placement only
fires on the submitted->starter fill transition and is idempotent-guarded on
stop_order_id, so nothing re-places these. This restores protection at each
ledger's current_stop and overwrites the stale stop_order_id.

Idempotent vs the live broker: skips any ticker that already has a working
SELL STP at Tiger. Default dry-run; pass --live to place.
"""
import sys
import yaml

from tools.auto_paper import state
from tools.broker.tiger import TigerClient

TICKERS = ["GO", "VAL", "MO"]
LIVE = "--live" in sys.argv

c = TigerClient()
acct = c.account_summary()
print(f"acct_fetched_at={acct.fetched_at}  mode={'LIVE' if LIVE else 'DRY-RUN'}")

# Tickers that already have a working SELL STP at the broker.
working_stp = {
    o["symbol"] for o in c.open_orders().output["orders"]
    if o.get("action") == "SELL" and str(o.get("order_type")).upper().endswith("STP")
}
held = {p["symbol"]: p["quantity"] for p in c.positions().output["positions"]}

print(f"{'TICKER':7}{'QTY':>7}{'STOP':>10}  RESULT")
print("-" * 50)
for t in TICKERS:
    doc = yaml.safe_load(open(state.ledger_path(t), encoding="utf-8"))
    ps = doc.get("position_state", {})
    stop = ps.get("current_stop")
    qty = int(held.get(t, 0) or 0)
    if qty <= 0:
        print(f"{t:7}{qty:>7}{stop:>10}  SKIP not-held"); continue
    if t in working_stp:
        print(f"{t:7}{qty:>7}{stop:>10}  SKIP already has working STP"); continue
    if not stop or stop <= 0:
        print(f"{t:7}{qty:>7}{str(stop):>10}  SKIP no current_stop"); continue
    if not LIVE:
        print(f"{t:7}{qty:>7}{stop:>10.2f}  (would place STP)"); continue
    try:
        entry = c.place_stop_loss(symbol=t, quantity=qty, stop_price=float(stop))
        sid = entry.output.get("order_id")
        state.record_stop_order_id(t, int(sid))   # overwrites the stale id
        print(f"{t:7}{qty:>7}{stop:>10.2f}  OK STP #{sid} (ledger updated)")
    except Exception as e:
        print(f"{t:7}{qty:>7}{stop:>10.2f}  ERROR {e}")
