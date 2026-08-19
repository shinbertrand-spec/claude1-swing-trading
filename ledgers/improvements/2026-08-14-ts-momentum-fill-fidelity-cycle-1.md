# FILL-FIDELITY CYCLE 1 — SCORED: **FAULT** (signals emitted, zero orders placed)

**Scored:** 2026-08-14 ~10:10 ET, by the /auto-paper-monitor session (cron), same
morning as the fault. Prereg: `ledgers/improvements/2026-08-06-fill-fidelity-tolerance-prereg.md`
(this is the scoring artifact that document required).

## Verdict per the pre-registered outcome table

| Check | Result |
|---|---|
| C1 — placement completeness | **FAULT — 0 / 8.** The 2026-08-13 rebalance emitted 8 signals (ARWR, INTC, LITE, MU, MXL, SNDK, STX, WDC — all `candidate_built: true`); **zero orders reached the broker.** |
| C2 — skip explicability | Not reached (no orders existed to fill or skip). |
| C3 — timing cost | Not reached. |

Outcome-table row applied: *"Signals emitted, zero orders placed → FAULT —
escalate immediately."* Per the C1 row: **the fault is the finding; fix it and
the next cycle becomes the eligible one.** Next eligible cycle: rebalance
**Mon 2026-09-14 → placement morning Tue 2026-09-15** (prereg pinned schedule).
Three cycles remain before the 2026-11-17 D3 review (09-14, 10-13, 11-11).

**No late re-placement was attempted.** The prereg pre-decides the response to
this branch; placing orders at ~10:15 instead of 09:35 would neither match the
certified fill mechanism nor rescue the cycle. Operator may of course fire
`/auto-paper` manually today if he wants the sleeve invested — that is an
operator call, explicitly outside this scoring.

## Evidence chain (all verifiable on disk)

- `ledgers/_auto_paper_runs/2026-08-14T13-35-11/00_signals.yml` — 8 selected,
  `signal_date: 2026-08-13`, written 13:37:34Z. (The C1 instrumentation added in
  the 2026-08-06 prereg addendum **worked exactly as designed** — without it this
  fault would have been indistinguishable from a VOID cycle.)
- Same run dir: `00_candidates.yml` present (8 sized candidates), but **no**
  `01_screener.yml`, no shell ledgers, no placement results; `_status.yml` has
  `phases_completed: []` — init never completed.
- Broker: `TigerClient.open_orders()` at 14:04Z → **0 open orders**;
  `journal/paper-auto/positions.json` untouched (last update 2026-07-27).
  No partial placement, no orphan-order risk.
- Task Scheduler: `ClaudeTradingAutoPaperEntry` last run 2026-08-14 09:35 ET,
  **LastTaskResult = 0** — the wrapper saw a clean exit. Silent failure.

## Root cause (from the headless session transcript `78c97b74…jsonl`)

1. 09:35:10Z — session runs `uv run python -m tools.auto_paper.run_entry --phase init`
   via the Bash tool **with the default 120s timeout**.
2. On a signal day, init = forced universe refetch (the 9:35 run always finds
   yesterday's cache ~24h old > 18h max) **plus** an 8-candidate build+size
   pass — wall-clock exceeds 120s.
3. 13:37:11Z — the harness auto-moved the command to the **background**.
4. 13:37:30Z — the session read interim output, said *"I'll be notified when
   the phase completes"*, and **ended its turn**. In headless `--print` mode,
   ending the turn terminates the session (exit 0) — which **killed the
   backgrounded python as an orphan** at ~13:37:35Z, seconds after it wrote
   the signal/candidate records and before the screener phase.

Why every prior day survived: 0-candidate days need only ~10 extra seconds past
the 120s mark; python finished before the session ended its turn. The failure
window only opens on days with candidates — i.e. **precisely on rebalance
placement mornings**, which is why the drought never self-diagnosed. This is
the likeliest mechanism behind the nine-week zero-fill drought called out in
the prereg.

## The fix (BLOCKED by write-deny — operator must apply)

`.claude/commands/auto-paper.md` is on the settings deny-list (by design; not
bypassed via shell per standing rule). Apply this to **Step 1** of
`.claude/commands/auto-paper.md`:

1. Change the Step-1 instruction line to:
   *"Run — **with an explicit 10-minute timeout on the Bash tool call
   (`timeout: 600000`), never the 120s default**:"*
2. Append after the expected-stdout line:

   > **Why the explicit timeout (2026-08-14 FAULT, fill-fidelity cycle 1).** On a
   > signal day, phase_init does a forced universe refetch (the 9:35 run always
   > finds the previous day's cache ~24h old) PLUS a per-candidate build+size
   > pass — wall-clock runs past the Bash tool's default 120s foreground window.
   > At 120s the harness auto-backgrounds the command; on 2026-08-14 the headless
   > `--print` session then ended its turn "awaiting the completion notification",
   > which in `--print` mode TERMINATES the session and kills the backgrounded
   > python as an orphan — init died mid-flight after emitting 8 signals, zero
   > orders placed. Two rules, both binding:
   >
   > 1. Always pass `timeout: 600000` on this Bash call so init stays foreground.
   > 2. If the command is ever backgrounded anyway (init exceeding 10 min is
   >    itself reportable — surface it), you MUST NOT end your turn while the
   >    phase is still running: poll the background task's output file
   >    (Read / TaskOutput) until the `PHASE_INIT_OK` / `PHASE_INIT_GATED`
   >    marker or a non-zero exit appears, then continue the steps. In headless
   >    mode, ending the turn kills the run.

Sizing basis for 600s: init was killed ~144s in with only screener/shell-ledger
file ops remaining; a signal-day init likely completes in 3–5 min. Optional
hardening (operator judgment, also deny-listed surface): pre-warm the data
cache before 09:35 via a small scheduled task so init never does the refetch
inline. The timeout fix alone closes the observed failure.

**Deadline:** patch must be in before **Mon 2026-09-14 09:35 ET** (next signal
day). Every intervening daily run is 0-candidate and safe, but the same latent
bug would kill cycle 2 identically if unpatched.

## Trials note

Operational reconciliation of a deployed strategy under its existing fill
model — **not a trial**; `trials.yml` untouched (per the prereg's scope
clause).

---

## ATTENDED ADDENDUM — 2026-08-14 ~10:45 ET (operator session)

**Fix applied:** the Step-1 timeout patch above was applied VERBATIM to `.claude/commands/auto-paper.md` at ~10:25 ET (operator-approved, applied from the attended Obsidian session — the write-deny is a wall against unattended self-modification, not against the operator). The 09-14 deadline is met with a month to spare.

**Operator-ordered manual placement (explicitly outside cycle-1 scoring, per this artifact's own carve-out):** Bertrand ordered "place those trades." The attended session ran the /auto-paper five-step protocol in-session (headless spawn blocked by the harness classifier; python phases run directly + 7 trade-skeptic and 32 panel-critic subagents fired per the command contract). Result at ~10:40 ET:
- **PLACED (3):** ARWR 645 (order 44278592430163968) · LITE 62 (44278592703447040) · SNDK 36 (44278593497091072). All panel verdicts = half_size_review; shadow mode → full size placed, verdicts logged for calibration.
- **REJECTED (4):** MU / STX / WDC — `gate_chain:correlation` (AI-momentum theme slot held by LITE; the B1 chain working live). MXL — stale `ledgers/paper-auto/MXL.yml` from the May era; idempotency guard refused (reconcile/archive that ledger before the next signal day).
- Screener had already BLOCKED INTC pre-panel (dilution: $15-20B common stock offering headlines).

**🔴 Live-demonstrated finding (was skeptic hypothesis, now fact):** SNDK BYPASSED the correlation theme gate because `tools/_themes/clusters.yml` predates the WDC→SanDisk spin-off — the map cannot count SNDK toward AI-momentum, so the book now holds LITE + SNDK = two expressions of the storage bet, one invisible to enforcement. **Queued fix (operator to apply): add SNDK (and consider MXL) to clusters.yml.** Until then the theme cap under-counts.

**Fidelity status unchanged:** cycle 1 remains **FAULT** (the manual fire does not score); next eligible cycle 09-14 signal → 09-15 placement under the patched command. Panel-calibration note: 7/7 half_size_review verdicts on one batch — first meaningful calibration cohort for the observe-gate.
