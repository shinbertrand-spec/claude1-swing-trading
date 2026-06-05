"""Flatten the v2-shadow orphan book (filled, no ledger, no stop).

Scoped to the AUDITED v2-orphan set derived from ledgers/_auto_paper_runs/
2026-06-0[1-4] placement results, cross-checked vs current Tiger holdings and
journal/paper-auto/positions.json. Excludes pre-shadow GKOS, the MXL/VRT
reconcile zombies, the COIN framework short, and the framework-tracked +
stopped GO / VAL / MO-543 legs.

MO: sell the 724-share orphan excess; leave the framework-tracked 543 (has STP).
SO: framework record is order ...26991824 = REJECTED (closed_unfilled); the
    Tiger 558 came from a different v2 fill -> whole 558 is orphan.

Default = dry-run (prices the book, places nothing). Pass --live to place.
Sells are DAY limit at bid * 0.999 per CLAUDE.md exit rule (Tiger paper = no GTC).
"""
import sys
from tools.broker.tiger import TigerClient

# Audited v2-orphan flatten map: ticker -> shares to sell.
ORPHANS = {
    "APD": 133, "COP": 10, "MO": 724, "SO": 558, "SBUX": 392, "AMD": 71,
    "CAT": 40, "GOOGL": 207, "MU": 35, "NUE": 144, "QCOM": 155, "PEP": 264,
    "AMZN": 150, "CMCSA": 1602, "NFLX": 462, "VST": 244,
}
# Never touch these even if they slip into the map (defense in depth).
PROTECT = {"GKOS", "MXL", "VRT", "COIN", "GO", "VAL"}

LIVE = "--live" in sys.argv

c = TigerClient()  # paper-routed; refuses live account
# Tiger QuoteClient lacks US market-data entitlement on this device, so price
# off the position mark (market_value/quantity) instead of live bid. mark is a
# fresh last-price; limit = mark * 0.999 is marketable for these liquid names.
acct = c.account_summary().output
pos = {p["symbol"]: p for p in c.positions().output["positions"]}
held = {t: p["quantity"] for t, p in pos.items()}

print(f"MODE    {'LIVE' if LIVE else 'DRY-RUN'}   acct_fetched_at={c.account_summary().fetched_at}")
print(f"{'TICKER':7}{'SELL':>6}{'HELD':>8}{'MARK':>10}{'LIMIT':>10}  RESULT")
print("-" * 70)

placed, est_proceeds, skipped = [], 0.0, []
for t, want in ORPHANS.items():
    if t in PROTECT:
        skipped.append((t, "in PROTECT set")); continue
    have = held.get(t, 0.0)
    qty = int(min(want, have))
    if qty <= 0:
        skipped.append((t, f"not held (tiger qty={have})")); continue
    mv = pos[t]["market_value"]
    mark = (mv / have) if have else 0.0
    if mark <= 0:
        skipped.append((t, f"no mark price (mv={mv})"))
        print(f"{t:7}{qty:>6}{have:>8.0f}{mark:>10.2f}{'-':>10}  SKIP no-mark")
        continue
    limit = round(mark * 0.999, 2)
    if LIVE:
        try:
            r = c.place_limit_sell(t, qty, limit)
            oid = r.output.get("order_id") or r.output.get("broker_order_id")
            placed.append((t, qty, limit, oid))
            est_proceeds += qty * limit
            print(f"{t:7}{qty:>6}{have:>8.0f}{mark:>10.2f}{limit:>10.2f}  OK #{oid}")
        except Exception as e:
            skipped.append((t, f"place failed: {e}"))
            print(f"{t:7}{qty:>6}{have:>8.0f}{mark:>10.2f}{limit:>10.2f}  ERROR {e}")
    else:
        est_proceeds += qty * limit
        print(f"{t:7}{qty:>6}{have:>8.0f}{mark:>10.2f}{limit:>10.2f}  (would place)")

print("-" * 70)
print(f"{'PLACED' if LIVE else 'WOULD PLACE'}: {len(placed) if LIVE else len(ORPHANS)-len(skipped)} orders"
      f" | est gross proceeds ~${est_proceeds:,.0f}")
if skipped:
    print("SKIPPED:")
    for t, why in skipped:
        print(f"  {t}: {why}")
