"""One-shot backfill (2026-07-24, Phase 1a): structured exit fields + unfilled flags.

The automated close path recorded exits only in the notes string, so 7 closed
ledgers were invisible to performance/live-vs-backtest readers. Verified split:

* INTU / COIN / MO / VAL — genuinely traded and closed; the reconcile/exits
  close note carries date + price + reason -> parsed into structured
  position_state.exit_price / exit_date / exit_reason.
* AMD / CAT / GOOGL — entry DAY orders expired unfilled (no trade ever
  happened; starter fill_price is the seeded limit) -> position_state.unfilled
  = true so readers exclude them instead of reporting phantom fills.

Idempotent: skips ledgers that already carry the structured fields.

Run: uv run python scripts/backfill_exit_fields.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from tools.auto_paper import reconcile as rc
from tools.auto_paper import state

TRADED = ["INTU", "COIN", "MO", "VAL"]
UNFILLED = ["AMD", "CAT", "GOOGL"]

CLOSE_RE = re.compile(
    r"Closed by auto_paper/(?:reconcile|exits)\s+on\s+(\d{4}-\d{2}-\d{2})\s+"
    r"at\s+\$([0-9.]+)\s*—\s*reason:\s*(.+?)(?:\n|$)"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = []
    for ticker in TRADED + UNFILLED:
        p = state.ledger_path(ticker)
        with open(p, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        ps = doc.setdefault("position_state", {})
        if ticker in UNFILLED:
            if ps.get("unfilled"):
                rows.append((ticker, "already flagged unfilled", ""))
                continue
            ps["unfilled"] = True
            action = "unfilled=true"
            detail = "entry DAY order expired unfilled"
        else:
            if ps.get("exit_price") is not None and ps.get("exit_date"):
                rows.append((ticker, "already structured", ""))
                continue
            matches = CLOSE_RE.findall(doc.get("notes") or "")
            if not matches:
                rows.append((ticker, "NO CLOSE NOTE FOUND - skipped, investigate", ""))
                continue
            exit_date, exit_price, exit_reason = matches[-1]  # last close wins
            ps["exit_price"] = float(exit_price)
            ps["exit_date"] = exit_date
            ps["exit_reason"] = exit_reason.strip()
            action = f"exit {exit_date} @ ${float(exit_price):.2f}"
            detail = exit_reason.strip()[:60]
        doc.setdefault("meta", {})
        doc["meta"]["updated_by"] = "manual/exit-backfill-2026-07-24"
        doc["meta"]["updated_at"] = rc._now_iso()
        rc._validate_against_schema(doc)
        if not args.dry_run:
            with open(p, "w", encoding="utf-8") as fh:
                yaml.safe_dump(doc, fh, sort_keys=False)
        rows.append((ticker, action, detail))

    print(f"{'ticker':8} {'action':30} detail")
    for t, a, d in rows:
        print(f"{t:8} {a:30} {d}")
    print("(dry run - nothing written)" if args.dry_run else "written + schema-validated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
