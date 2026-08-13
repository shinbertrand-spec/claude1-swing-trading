# 2026-07-27 — GO composer exit rejected by broker (holdings encumbered by resting STP)

**Status:** FIXED 2026-08-13 — Option 1 (cancel-stop-then-sell) implemented in `tools/auto_paper/exits.py` + expiry-revert stop re-arm in `reconcile.py`, ahead of the 08-14 cycle-1 placements. See `2026-08-13-cancel-stop-then-sell-implemented.md`. *(Was: OPEN — structural bug in the 2026-07-27 pending_close redesign. Position is safe (STP protection intact); sell-discipline exits cannot execute on Tiger paper while a full-size stop rests.)*

## What happened

1. 10:00:26 ET monitor tick: `tools.auto_paper.exits` composer returned `sell_50`
   for GO (`violations_2 (count=2)`), treated as full close per v1. Placed
   limit-sell #44074483903777792 for 3,113 sh @ $9.34 and transitioned the
   ledger to `pending_close` — per the post-mortem redesign, the protective
   STP #44043577126043648 ($8.43, 3,113 sh) was deliberately left resting.
2. Tiger paper **rejected the sell immediately** (status `EXPIRED`, 0 filled,
   `order_time == update_time`): *"The order quantity you entered exceeds your
   current holdings."* The resting STP encumbers all 3,113 shares, so a second
   full-size SELL is refused at submission.
3. 10:30 ET monitor tick (this run): GO sits in `pending_close`, excluded from
   starter processing (no composer evals, no ratchet). Broker verified: still
   holds 3,113 sh (~$9.38, unrealized +$4,166), only the STP resting.

## Self-heal path (no action needed today)

`reconcile.py` pending_close processing (~line 2094): sell order gone from
broker + not in today's fills → reverts `pending_close` → `starter`, clears
`pending_sell_order_id`, leaves the protective stop alone. The 16:30 ET
`/auto-paper-reconcile` will restore GO to `starter`.

## The structural bug

The 2026-07-27 post-mortem fix excluded STP SELLs from the idempotency guard
(`_open_sell_tickers`) so composer exits would no longer be suppressed as
`sell_pending_duplicate`. The redesign assumes a protective STP and an exit
limit-sell can rest concurrently against the same shares, with the reconciler
cancelling the stop after fill confirmation. **Tiger paper does not allow
this** — sell quantity is validated against unencumbered holdings at
submission, so every composer exit on a fully stop-protected position is
rejected on arrival.

Net effect: sell-discipline is still disabled on this track, now via broker
rejection instead of guard suppression, plus a daily churn loop:

> composer fires → sell rejected (EXPIRED) → pending_close (position skips
> evals/ratchet for the rest of the session) → 16:30 revert to starter →
> next day repeat.

## Fix options (operator decision — code change outside monitor scope)

1. **Cancel-stop-then-sell** (mirror `stop_ratchet` mechanics): cancel the
   resting STP, place the exit limit-sell, and on placement failure re-arm
   the stop; record unprotected state in `notes` + clear `stop_order_id` so
   the next pass retries. Bounded unprotected window, same recovery contract
   the ratchet already uses.
2. **Size the exit to unencumbered qty** — not viable here (STP encumbers
   100% of the position → exit qty would be 0).
3. **Reduce the STP first** (cancel/replace at qty 0 equivalent) — same as
   option 1 with more steps.

Option 1 is the code path most consistent with the original Session-3 design
(the skill's Step 1 item 5 always intended the stop to be cancelled as part
of the exit) and with the existing ratchet recovery contract.

## Evidence

- Rejected order: #44074483903777792, `OrderStatus.EXPIRED`, reason
  "The order quantity you entered exceeds your current holdings",
  qty 3,113 / filled 0, limit $9.34, order_time 1785160826000 (10:00:26 ET).
- Resting stop: #44043577126043648, STP SELL 3,113 @ $8.43, status HELD.
- Broker positions (14:30 UTC): GO 3,113 sh, avg cost $8.0419, mv $29,199.94.
- Ledger note (GO.yml): "Pending close by auto_paper/exits on 2026-07-27 —
  limit-sell @ $9.3400, reason: sell_decision/sell_50 (violations_2 (count=2))."
- `get_filled_orders(2026-07-27, symbol=GO)` → 0 orders.
