"""One-shot SO ledger correction (2026-07-27, post-mortem external review follow-up).

Broker-history provenance pull (read-only, 2026-07-27; window 2026-06-01..11,
Tiger paper) proved the paper-auto SO entry never filled:

* Ledger's BUY #43452026991822848 (419 sh, limit $89.21, placed 2026-06-02) is
  ABSENT from filled-order history -> genuinely expired unfilled, exactly as the
  ledger note ("Order expired unfilled on 2026-06-04") and positions.json
  (`closed_unfilled`) always said.
* The SELL @ $91.40 cited by the 2026-06-07 manual exit_price backfill is real
  but belongs to a DIFFERENT, framework-external SO round trip: BUY
  #43445909223260160 (558 sh, filled avg $89.0653, 2026-06-02 pre-open) ->
  SELL #43476803534210048 (558 sh, filled $91.40). Neither order ID appears
  anywhere in this repo -> placed outside the framework (manual app trade on
  the shared paper account). Its +$1,303 is NOT paper-auto P&L.

Fix: strip the misattributed exit fields, flag unfilled=true so all readers
exclude SO, and record the provenance in notes. Paper-auto realized net becomes
-$12,094 over 8 filled trades (2W/6L).

Idempotent. Run: uv run python scripts/so_unfilled_correction.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from tools.auto_paper import reconcile as rc
from tools.auto_paper import state

PROVENANCE_NOTE = (
    "PROVENANCE CORRECTION 2026-07-27: broker-history pull (2026-06-01..11) shows "
    "BUY #43452026991822848 (419 sh) never filled — expired unfilled as noted. The "
    "SELL @ $91.40 used by the 2026-06-07 manual backfill was a framework-external "
    "558-sh round trip (BUY #43445909223260160 @ $89.0653 -> SELL #43476803534210048), "
    "not this position. exit_price/exit_reason removed; unfilled=true. The +$918 "
    "previously attributed here was phantom."
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    p = state.ledger_path("SO")
    with open(p, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    ps = doc.setdefault("position_state", {})

    if ps.get("unfilled") and ps.get("exit_price") is None:
        print("SO already corrected — nothing to do")
        return 0

    removed = {k: ps.pop(k, None) for k in ("exit_price", "exit_date", "exit_reason")}
    ps["unfilled"] = True
    notes = (doc.get("notes") or "").rstrip()
    doc["notes"] = f"{notes}\n{PROVENANCE_NOTE}" if notes else PROVENANCE_NOTE
    doc.setdefault("meta", {})
    doc["meta"]["updated_by"] = "manual/so-provenance-correction-2026-07-27"
    doc["meta"]["updated_at"] = rc._now_iso()
    rc._validate_against_schema(doc)
    if not args.dry_run:
        with open(p, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, sort_keys=False)
    print(f"removed: { {k: v for k, v in removed.items() if v is not None} }")
    print("unfilled=true set; provenance note appended")
    print("(dry run - nothing written)" if args.dry_run else "written + schema-validated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
