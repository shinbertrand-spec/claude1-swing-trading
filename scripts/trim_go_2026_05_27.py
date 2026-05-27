"""Trim the GO paper-auto position to half — discretionary risk-management.

Decided 2026-05-27 in response to Phase 1 screener retrospective: GO is a
named defendant in Jones v. GO (N.D. Cal., 26-cv-02291), an active securities
class action filed in late April 2026. CLAUDE.md disqualifier rule treats
this as a one-strike block, but the position was placed before the screener
existed.

Bertrand's call (from AskUserQuestion 2026-05-27): trim to half.
6,225 shares → 3,113 shares (close 3,112 shares).

This script:
1. Reads the current GO paper-auto ledger + positions.json entry
2. Places a GTC limit-sell at Tiger for 3,112 shares at bid - 0.1%
   (~$7.91 against the 2026-05-26 close of $7.92). GTC because market is
   closed when this is invoked; the order persists until filled.
3. Updates the ledger's position_state.starter.shares from 6225 to 3113
4. Updates positions.json shares from 6225 to 3113
5. Appends a note to ledger.meta.notes documenting the trim rationale

Once filled the EOD reconciler will see the closed sell and surface the
realized P&L; the residual 3,113 shares remain subject to the existing
$6.41 stop until the framework decides to fully exit.

Run with --dry-run first to see what would happen.

Usage::

    uv run python scripts/trim_go_2026_05_27.py --dry-run
    uv run python scripts/trim_go_2026_05_27.py             # for real
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.broker.tiger import TigerClient  # noqa: E402


GO_LEDGER = ROOT / "ledgers" / "paper-auto" / "GO.yml"
POSITIONS_JSON = ROOT / "journal" / "paper-auto" / "positions.json"

# Trim parameters
TRIM_QTY = 3112           # close 3,112 of 6,225 (rounds the residual to 3,113)
LIMIT_PRICE = 7.91        # bid - 0.1% off 2026-05-26 close of $7.92
TRIM_REASON = (
    "Discretionary trim 2026-05-27. Phase 1 screener retrospective surfaced "
    "active securities class action (Jones v. GO, N.D. Cal., 26-cv-02291). "
    "CLAUDE.md disqualifier applies retroactively. Trim to half per Bertrand."
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Trim GO paper-auto position to half.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print plan without placing the order or mutating ledgers.",
    )
    args = parser.parse_args()

    # 1. Load current ledger + positions.json entry
    if not GO_LEDGER.exists():
        print(f"ERROR: ledger missing at {GO_LEDGER}")
        return 1
    with open(GO_LEDGER, encoding="utf-8") as fh:
        ledger = yaml.safe_load(fh)
    current_shares_ledger = ledger["position_state"]["starter"]["shares"]

    with open(POSITIONS_JSON, encoding="utf-8") as fh:
        pj = json.load(fh)
    go_entry = next((p for p in pj["positions"] if p["ticker"] == "GO"), None)
    if go_entry is None:
        print("ERROR: GO not found in positions.json")
        return 1
    current_shares_pj = go_entry["shares"]

    if current_shares_ledger != current_shares_pj:
        print(
            f"WARN: ledger shares ({current_shares_ledger}) != positions.json "
            f"shares ({current_shares_pj}). Proceeding with positions.json view."
        )
    current_shares = current_shares_pj
    new_shares = current_shares - TRIM_QTY

    print("=" * 70)
    print("GO trim plan")
    print("=" * 70)
    print(f"  Current shares:     {current_shares:,}")
    print(f"  Trim qty (close):   {TRIM_QTY:,}")
    print(f"  Residual shares:    {new_shares:,}")
    print(f"  Limit price:        ${LIMIT_PRICE:.2f}")
    print(f"  Time-in-force:      DAY (Tiger paper does not allow GTC)")
    print(f"  Estimated proceeds: ${TRIM_QTY * LIMIT_PRICE:,.2f}")
    print(f"  Reason:             {TRIM_REASON}")
    print()

    if args.dry_run:
        print("[--dry-run] Would place the order and update ledger + positions.json.")
        return 0

    # 2. Place the DAY limit-sell via Tiger paper client
    print("Placing DAY limit-sell at Tiger paper account...")
    try:
        from tigeropen.common.util.order_utils import limit_order
        c = TigerClient()  # paper-routed; refuses live
        contract = c._tc.get_contract(symbol="GO")
        if contract is None:
            print("ERROR: no Tiger contract returned for GO")
            return 1
        # Tiger paper account doesn't support GTC (only DAY). DAY is fine
        # in practice — when placed pre-market, Tiger queues the order for
        # the next regular session open.
        order = limit_order(
            account=c._account,
            contract=contract,
            action="SELL",
            quantity=TRIM_QTY,
            limit_price=LIMIT_PRICE,
            time_in_force="DAY",
        )
        order_id = c._tc.place_order(order)
    except Exception as exc:
        print(f"ERROR: broker call failed: {exc}")
        return 1
    print(f"  Order placed: #{order_id}")

    # 3. Update the ledger
    ledger["position_state"]["starter"]["shares"] = new_shares
    if "notes" not in ledger.get("meta", {}):
        ledger["meta"]["notes"] = []
    if not isinstance(ledger["meta"].get("notes"), list):
        ledger["meta"]["notes"] = []
    ledger["meta"]["notes"].append({
        "date": "2026-05-27",
        "event": "discretionary_trim",
        "broker_order_id": int(order_id),
        "from_shares": current_shares,
        "to_shares": new_shares,
        "limit_price": LIMIT_PRICE,
        "reason": TRIM_REASON,
    })
    with open(GO_LEDGER, "w", encoding="utf-8") as fh:
        yaml.safe_dump(ledger, fh, sort_keys=False)
    print(f"  Ledger updated: {GO_LEDGER}")

    # 4. Update positions.json
    go_entry["shares"] = new_shares
    pj["updated"] = "2026-05-27T00:00:00+00:00"
    with open(POSITIONS_JSON, "w", encoding="utf-8") as fh:
        json.dump(pj, fh, indent=2)
    print(f"  positions.json updated: {POSITIONS_JSON}")

    print()
    print("Done. The DAY sell queues for the next regular session open.")
    print("Run /auto-paper-reconcile after market close to capture the realized fill.")
    print("If the order DAY-expires without filling (gap-down), re-run this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
