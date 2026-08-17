# 2026-08-17 — Post-entry grace period (backtest parity) + monitor-tick stop arming

**Closes the two 🔴 items from `2026-08-14-monitor-same-day-composer-exits.md`
before the 09-14/09-15 scored cycle.** Operator GO 2026-08-16 ("go" on the
three-fix plan); implemented attended 2026-08-17.

## Fix 1 — 3-bar post-entry sell suppression in the live monitor

**Defect:** the backtest simulator only consults the sell-composer once
`offset > grace_period_bars` (fill bar = offset 0; `tools/backtest/sell_aware.py`
`SellPolicy.grace_period_bars = 3`, `tools/backtest/simulator.py` step 5). The
live monitor had NO equivalent: on 2026-08-14 the 13:32 ET tick evaluated
three ~2h-old fills against full-day OHLCV that predates their entries and
same-day-exited MU/STX/WDC (`violations_1` + `sell_into_strength 0.8` — the
pre-entry run-up ts_momentum SELECTS FOR; a trend entry near-always looks
like "sold into strength" on its own fill day). Unfixed, live hold-period ≈
hours vs backtest ≈ weeks — the fidelity cycle would have measured the
composer's exit policy, not ts_momentum.

**Fix (`tools/auto_paper/exits.py`):**

- New module constant `GRACE_PERIOD_BARS = 3` (parity-documented).
- `_evaluate_one` computes `bars_held` (simulator offset semantics: count of
  df bars dated ≥ fill_date, minus 1; clamped ≥ 0; unparseable index or a
  fetch missing the fill-day bar fail-safes to 0 = inside grace). Also now
  drives sell_into_strength's `days_in_move` (same number as before, hoisted).
- `evaluate_exits`: a composer SELL verdict (`sell_50/75/100`) with
  `bars_held <= GRACE_PERIOD_BARS` is **recorded but not transacted** — routed
  through the non-transacting branch with `grace_suppressed`, the composer's
  TRUE action preserved in `sell_eval_history` (+ `grace_suppressed: true`,
  `bars_held: N` — free calibration data), `ExitResult.grace_suppressed=True`,
  reason string explains the suppression. No broker calls; stop untouched.
- **Watchdog exemption (load-bearing):** the stop-breach watchdog now runs
  even when the composer already said `sell_100`, if that verdict was
  grace-suppressed — a broker-confirmed failed stop (close < still-open STP)
  forces the exit on ANY bar. Grace suppresses composer discretion only,
  never stop protection. Protective stops themselves are unaffected.
- Schema (`ledgers/_schema/ledger.schema.json`): `sell_eval_entry` gains
  optional `grace_suppressed` (bool) + `bars_held` (int ≥ 0). `action` stays
  in-enum (the composer's true verdict).

## Fix 2 — fill promotion + stop arming at every monitor tick

**Defect:** the 08-14 fills sat at the broker with NO protective stop from
10:13→13:32 ET — promotion + stop placement only happened at the 16:30
reconcile (the monitor session closed the gap by running `reconcile_today`
manually).

**Fix (`.claude/commands/auto-paper-monitor.md`):** new **Step 0** runs
`reconcile_today(client=..., dry_run=...)` FIRST at every tick — promotes
filled `submitted` entries to `starter` with stops sized to actual fills,
closes stop-outs before the refresh (internal load-bearing order), re-arms
DAY-expired stops, resolves pending_close. A morning fill is now
stop-protected by the next tick (≤30 min), not at the close. Old Step 1a
(separate `reconcile_stop_outs` + `refresh_starter_stops` calls) RETIRED —
subsumed by Step 0; calling them separately would double broker round-trips.
Intraday safety is by-design and was live-verified 2026-08-14 13:32 ET:
still-open DAY orders → `still_open` no state change; FIX-4 held-symbol
guard prevents premature expiry. The 16:30 `/auto-paper-reconcile` stays
authoritative for EOD (skip rows, execution attribution). **No Python change
needed** — `reconcile_today` already did all of this; the monitor just never
called it.

Interlock: Step 0 promotion means the composer can see a fresh fill at the
SAME tick — which is exactly what Fix 1's grace window makes safe.

## Fix 3 — regression tests (Friday's own data shape)

`tests/test_auto_paper_exits.py`, new section (5 tests; file 28/28 green):

1. `test_grace_period_suppresses_day0_composer_sell` — the MU replay: bar-0
   fill, composer sell_50 → recorded w/ flag+bars_held=0, nothing placed,
   stop NOT cancelled, ledger stays starter.
2. `test_grace_period_boundary_bar3_suppressed` — offset 3 = last suppressed bar.
3. `test_grace_period_expired_bar4_transacts` — offset 4 → normal
   cancel-stop-then-sell path unchanged (pending_close).
4. `test_watchdog_overrides_grace_period` — failed stop on bar 0 → sell_100
   forced through despite grace.
5. `test_grace_dry_run_records_verdict_no_broker`.

`_seed_starter` gained a `fill_date` param (defaults far-past so existing
tests never engage grace). **Full suite: 2131 passed, 0 failed (2026-08-17).**

Incidental repair found by the suite run: `tests/test_earnings_calendar.py`
had two hardcoded "future" dates (2026-08-13) compared against the real
system clock — they expired 08-13 and started failing on age alone this
weekend. Repaired clock-relative (`date.today() ± timedelta`); unrelated to
the grace/stop changes but fixed so the suite is honestly green.

## What this does NOT change

- The composer's logic/thresholds — certified in backtest WITH the grace
  period; this restores parity, not a redesign.
- Exits on scored positions past bar 3 — unchanged.
- The stop ratchet — trail updates run from bar 0 in the simulator too
  (step 1), so day-0 ratcheting IS parity; untouched.
- The 2026-08-14 forced positions' outcomes — those exits were sanctioned
  mechanics on fidelity-quarantined tickets; MU/STX/WDC stay closed.

## Watch items for the 09-15 cycle

- First scored cycle under: patched auto-paper.md timeout + Step-0 tick
  reconcile + grace window. Expect `grace_suppressed` rows in
  sell_eval_history during the first 3 bars — audit them (calibration data),
  don't panic at composer sell verdicts that don't transact.
- ARWR (open, past grace since 08-19) is unaffected by the window and
  remains the first live test of a composer exit / ratchet on a scored-path
  position.
