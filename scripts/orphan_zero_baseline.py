"""One-shot: declare the paper-auto orphan-zero baseline (2026-06-07).

Snapshots the broker holdings, reconciles against the paper-auto starter ledgers
(dynamic PROTECT = starter set), and persists the inventory to
journal/paper-auto/orphan-zero-baseline-2026-06-07.yml.

GKOS was the last known pre-fix orphan (flattened 2026-06-05). The v2
validate-before-place gate (pipeline.py:369) stops NEW orphans. This baseline is
the clean-slate declaration that the pre-fix orphan population is fully drained
BEFORE the Mode-A reconciler (Step 3) goes live -- the reconciler must not run on
top of an unknown orphan set.

If orphan_set is non-empty OR any ledger is corrupt, the script STOPS with a
non-zero exit and surfaces the survivors -- do not proceed to Step 3.

Run:  uv run python scripts/orphan_zero_baseline.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import yaml

from tools.auto_paper import orphan_check as oc
from tools.broker.tiger import TigerClient

OUT = "journal/paper-auto/orphan-zero-baseline-2026-06-07.yml"


def main() -> int:
    client = TigerClient()  # paper-routed; refuses live
    positions = client.positions().output["positions"]
    holdings = {p["symbol"].upper(): float(p["quantity"]) for p in positions}

    scan = oc.scan_ledgers()
    rep = oc.compute_orphans(holdings, scan=scan)

    doc = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "broker_holdings": rep.broker_holdings,
        "open_ledgers": rep.starter_tickers,        # starter == the active-held state
        "protect_set": rep.protect_set,             # dynamic PROTECT = starter set
        "orphan_set": rep.orphan_set,
        "corrupt_ledgers": [list(c) for c in rep.corrupt_ledgers],
        "declaration": "Pre-fix orphan inventory complete as of 2026-06-07",
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)

    print(f"broker_holdings : {rep.broker_holdings}")
    print(f"starter ledgers : {rep.starter_tickers}")
    print(f"protect_set     : {rep.protect_set}")
    print(f"orphan_set      : {rep.orphan_set}")
    print(f"corrupt_ledgers : {rep.corrupt_ledgers}")
    print(f"persisted -> {OUT}")

    if not rep.is_clean:
        print("\nSTOP: baseline is NOT clean. Surface survivors before Step 3:")
        if rep.orphan_set:
            print(f"  ORPHANS (broker holds, no starter ledger): {rep.orphan_set}")
        if rep.corrupt_ledgers:
            print(f"  CORRUPT LEDGERS: {rep.corrupt_ledgers}")
        return 2

    print("\nORPHAN-ZERO CONFIRMED: orphan_set empty, no corrupt ledgers. OK for Step 3.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
