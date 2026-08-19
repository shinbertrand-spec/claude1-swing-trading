# 2026-08-14 — Monitor tick 13:32 ET: composer same-day-exits 3 of 4 first ts_momentum fills

## What happened

First `/auto-paper-monitor` tick after the first-ever ts_momentum fills (placed
manually 2026-08-14 ~10:13 ET, see `2026-08-14` fill-path notes).

1. **Early reconcile (deliberate, out-of-schedule):** monitor found all 7
   placements still `submitted` and the 4 filled names (ARWR/MU/STX/WDC,
   ~$168k notional) holding at the broker **with no protective stops** — stops
   are only placed by the 16:30 ET reconcile. Ran `reconcile_today` at the
   monitor tick: 4 promoted to `starter` with stops placed (~3h early);
   LITE/SNDK/MXL still open at broker, no state change.
2. **Composer pass then same-day-exited 3 of the 4:**
   - MU: `sell_50` → SELL 43 @ 963.29 limit (vs fill 969.25, **−0.6%**), stop #44279987436274688 cancelled, `pending_close`
   - STX: `sell_50` → SELL 45 @ 969.15 (vs fill 948.79, +2.1%), stop cancelled, `pending_close`
   - WDC: `sell_50` → SELL 84 @ 497.79 (vs fill 498.205, −0.1%), stop cancelled, `pending_close`
   - ARWR: `sell_1_3` = noisy hold (not in v1 SELL_ACTIONS) — position kept, stop intact, ratchet no_change (+2.0%)
   - Triggers on all three: `violations_1 (count=1)` + `sell_into_strength (fraction=0.8)`, confidence MEDIUM.

Mechanics are AS CURRENTLY DESIGNED: the 2026-08-13 cancel-stop-then-sell path
(commit `1e1e2eb`) supersedes the post-NFLX "resting stop suppresses composer
sells" behavior — composer sells now cancel the broker-confirmed stop and place
the exit. No orders were cancelled or second-guessed by the monitor session.

## Context that tempers this

MU/STX/WDC are the 08-14 **operator-forced** placements (`operator_forced`,
excluded from all fidelity scoring per the receipt), and the operator accepted
"exits system-managed (stops + cancel-stop-then-sell + rebalance drop-outs)".
So today's three exits are sanctioned mechanics on out-of-scope tickets. The
open issue is FORWARD-looking: ARWR (a properly-placed name, spared only
because `sell_1_3` isn't executable in v1) and every future scored cycle
(09-15 onward) will face the same day-0 evaluation.

## Why this needs review (fidelity, not mechanics)

- The detectors ran on **day-0 of the position** against OHLCV that predates
  entry. `sell_into_strength` fired on the pre-entry run-up — the very
  momentum ts_momentum SELECTS FOR. A trend-following entry will near-always
  look like "10–15% in 2–3 days" on its fill day.
- The backtest side has a post-entry suppression window: `tools/backtest/runner.py`
  sell-aware mode suppresses the sell-decision for N bars after entry
  (default 3). The live monitor has **no** equivalent — it evaluated on bar 0.
- Net effect: live hold-period ≈ hours vs backtest hold ≈ weeks. If this
  stands, the fill-fidelity cycle (C1/C2/C3, next scored at-bat 09-14→09-15)
  measures the composer's exit policy, not ts_momentum.

## Decisions for the operator

1. Cancel the three pending LMT sells (Tiger) and re-arm stops, or let them
   fill and accept the same-day round-trip? (They may fill before 16:30.)
2. Add a post-entry suppression window (mirror the backtest's 3-bar default)
   to `evaluate_exits`, and/or exempt/param the discretionary-lineage
   detectors on the quant track?
3. Consider wiring the fill-promotion + stop-placement into the monitor tick
   permanently — the 10:13→13:32 ET stop-less gap is structural while stops
   wait for the 16:30 reconcile.

No code changed in this session; state changes were reconcile + composer
outputs only.
