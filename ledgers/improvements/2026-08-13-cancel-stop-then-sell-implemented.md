# 2026-08-13 — cancel-stop-then-sell implemented (closes 2026-07-27 composer-exit broker rejection)

**Status:** SHIPPED — full suite 2126 passed. Implemented on operator GO (Bertrand, 2026-08-13, pre-cycle-1) so the 08-14 placements start the fidelity cycle on a working exit path instead of a known-broken one.

## What changed

Option 1 from `2026-07-27-go-composer-exit-broker-rejected.md` (the option that doc already
recommended), mirroring `stop_ratchet` mechanics, plus a rejection guard the doc's incident
proved necessary:

1. **`tools/auto_paper/exits.py` — cancel-stop-then-sell.** Before placing a composer
   exit limit-sell, a **broker-confirmed** resting protective STP (ledger
   `stop_order_id` present AND in the open-orders STP set) is cancelled first, with
   the ratchet's `accepted` check. Cancel refusal/failure → exit NOT placed, position
   keeps its stop, composer retries next tick. Tiger validates SELL quantity against
   *unencumbered* holdings at submission, so the old order (sell placed while the
   full-size stop rested) was rejected on arrival — every composer exit on every
   stop-protected position.
2. **Re-arm on placement failure** (`_rearm_stop_after_failed_exit`): if the exit
   placement raises after the stop was cancelled, the stop is re-placed at
   `position_state.current_stop` (fallback `setup_classification.stop_price`).
   Re-arm failure follows the ratchet recovery contract: `stop_order_id` cleared +
   `TEMPORARILY UNPROTECTED` notes line; next ratchet/reconcile pass retries.
3. **Submission-rejection guard** (`_sell_gone_at_broker`): Tiger encodes an
   at-submission rejection as an instantly-EXPIRED order with a valid order_id and
   filled=0 (GO order #44074483903777792) — `place_limit_sell` raises nothing. The
   guard checks open+filled immediately after placement; an order in neither list
   was rejected → error result, stop re-armed, **no pending_close transition** (the
   2026-07-27 phantom). Any read failure stands the guard down (fail-quiet;
   reconcile owns the slow path).
4. **Watchdog reorder:** the zombie-stop cancel now happens via the same
   pre-placement path (it previously cancelled AFTER placing, so the watchdog's
   forced exit inherited the same encumbrance rejection).
5. **`tools/auto_paper/reconcile.py` — expiry-revert re-arm:** the
   `pending_close` → `starter` revert (exit expired unfilled) now calls
   `_ensure_stop_for` — under the new flow the reverted starter may be naked.
   `_ensure_stop_for` is idempotent (detects a live STP and keeps it), so pre-fix
   reverts where the stop genuinely survived are handled identically.
6. Ledger recording: on a successful exit placement after a stop cancel, the dead
   `stop_order_id` is cleared and a `cancel-stop-then-sell` notes line is written;
   `current_stop` is kept as price memory for the reconciler's re-arm.

## Tests

- 4 new tests in `tests/test_auto_paper_exits.py`: cancel-before-place ordering +
  ledger clearing; cancel-refusal blocks the exit (stop kept); placement-failure
  re-arm at the ledger stop price; submission-rejection detection with stop re-arm
  and NO pending_close (the GO shape, replayed).
- All 53 pre-existing exits/reconcile tests pass unchanged — the new cancel only
  fires on a **broker-confirmed** open stop, so read-failure environments degrade
  to the old behavior (sell placed, guard stands down) rather than breaking.
- Full suite: **2126 passed**.

## Residual gaps (honest)

- The `pending_close` reconcile paths (`exit_filled` / `exit_expired_reverted`)
  still have no direct test harness — the reconcile change reuses the
  already-tested idempotent `_ensure_stop_for` in the exact pattern of the Mode A
  revert, and rides the full-suite green. A dedicated harness is a
  future-session item.
- On a broker open-orders read failure the pre-cancel stands down and the exit is
  placed against a possibly-resting stop (old rejection shape); the
  submission-rejection guard usually also can't confirm in that state, so the
  phantom degrades to the pre-fix self-heal loop (revert next reconcile) rather
  than being caught same-tick. Accepted: fail-quiet beats fail-active on
  unconfirmed broker state.
- Live-fire validation pending: first real composer exit on a stop-protected
  position during cycle 1 should be watched end-to-end (accept-ledger row 7
  discipline extends to the first exit, not just the entries).
