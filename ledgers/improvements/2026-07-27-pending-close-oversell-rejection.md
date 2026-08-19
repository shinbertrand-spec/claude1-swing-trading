# pending_close "both-resting" contract fails on Tiger paper — oversell rejection

**Date:** 2026-07-27 (found by the 11:00 ET /auto-paper-monitor run)
**Severity:** HIGH — composer sell-discipline is still effectively disabled for
stop-protected positions, one layer deeper than the pre-d1955d2 regression.

## What happened

The 10:00 ET monitor tick fired the composer on GO (`sell_50`, violations_2 —
signal active since 07-24) and, per the d1955d2 post-mortem redesign, placed the
exit limit-sell (3,113 @ $9.34, #44074483903777792) **without cancelling the
resting protective STP** (3,113 sh, #44043577126043648). The redesign's contract:
both orders rest; the reconciler cancels the stop after confirming the exit fill.

Tiger paper **rejected the second SELL outright**:
`reason: "The order quantity you entered exceeds your current holdings."`
(3,113 STP + 3,113 LMT = 6,226 sell exposure vs 3,113 held). The rejection is
encoded as `status: EXPIRED, filled: 0` — it looks like a DAY expiry, not an
error, so `evaluate_exits` returned success, the ledger moved to `pending_close`
"awaiting reconcile fill confirmation", and nothing surfaced.

## The standing loop (until fixed)

1. Monitor tick: composer SELL → limit-sell placed → instant oversell rejection
   (EXPIRED/filled=0) → ledger `pending_close`.
2. 16:30 reconcile: pending sell unfilled → reverts `pending_close` → `starter`.
3. Next session: repeat. The exit never executes; the position exits only via
   the stop or operator action — exactly the pre-fix behavior, but now with a
   daily rejected order and a misleading "awaiting fill confirmation" note.

## Remediation applied today (manual, 15:04 UTC)

- Cancelled STP #44043577126043648 (accepted).
- Placed limit-sell 3,113 GO @ $9.39 (#44074984830683136) → **FILLED avg $9.4350**.
- Realized ≈ +$4,374 vs $8.03 entry (+17.5%); vs initial stop $6.41 ≈ +0.87R.
- Ledger + positions.json updated: `pending_sell_order_id` → #44074984830683136,
  `stop_order_id` cleared, `pending_close` retained so the 16:30 reconcile
  confirms the fill via the normal `_close_ledger_from_pending` path and records
  the Phase-3 calibration outcome.
- Broker verified flat after: 0 positions, 0 open orders.

## Recommended code fix (not applied — post-mortem-reviewed safety path, needs tests)

The "both-resting" design cannot work on Tiger paper at full size. Options:

1. **Cancel-then-place with recovery** (mirror `stop_ratchet`'s mechanic):
   cancel the STP, place the exit; if the place fails after a successful
   cancel, record the unprotected state in `notes`, clear `stop_order_id`,
   retry next tick. Reinstates the original Session-3 ordering the skill doc
   still describes. The unprotected window is seconds, vs. the current outcome
   (exit never happens).
2. Alternatively, detect the rejection at point of sale: after
   `place_limit_sell`, poll the order once; `EXPIRED + filled=0 + reason`
   non-empty within seconds of placement = rejection → surface `error`, do NOT
   transition to `pending_close`.

Option 1 fixes the exit; option 2 only makes the failure loud. Do 1, keep 2 as
a belt-and-suspenders assertion.

Also note: `_pending_close` reconcile + `reconcile_stuck_closing` behaved as
designed here — the defect is confined to the exits placement contract.
