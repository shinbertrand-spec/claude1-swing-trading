# Incident — NFLX paper-auto naked short (remediation record, 2026-07-24)

**Classification: BUG ARTIFACT. The P&L below is NOT strategy P&L and MUST NOT
enter realized performance stats.** The NFLX paper-auto ledger was deliberately
closed WITHOUT `position_state.exit_price` so the performance module excludes it
by design. This note is the sole record of the flatten P&L.

## What happened
The paper-auto reconciler measured broker holdings by magnitude (`abs()`), so a
negative broker quantity read as long shares. An unintended NFLX short — seeded
via a `connors_rsi2` entry on 2026-06-05 (465 sh) that desynced — was classified
`stuck_closing`, flipped back to a starter, and armed with a STP SELL that
*deepened* the short on each reconcile/monitor pass. Observed doubling:
7,440 → 14,880 → **−29,760** (avg short price $73.2704), self-arrested only at
Tiger paper's margin ceiling. Monitor + Reconcile crons were disabled and entries
gated (`cron_gate.json`, reason `nflx_naked_short_guard_bug`) on 2026-07-06 to
stop the compounding.

## The flatten (bug-artifact close)
- Order **#44037343661016064** — BUY 29,760 NFLX, DAY marketable limit $70.00,
  placed 2026-07-24 pre-open; filled 29,760 @ **$68.9225**.
- Broker NFLX position: −29,760 → **0 (flat)**, verified via signed positions pull.
- **Realized on the short = (73.2704 − 68.9225) × 29,760 ≈ +$129,393.**
- This gain is an accident of NFLX drifting down while the erroneous short was
  open (it was −$139k underwater on 07-03). It reflects the *bug*, not any
  strategy edge. **Excluded from `connors_rsi2` / paper-auto realized stats.**

## Account state after flatten (Tiger paper …3806)
- Net liquidation ≈ **$1,137,517**; cash $1,109,687 (fell ~$2.05M = the
  buy-to-close cost, consistent with the fill).
- Remaining position: **GO** 3,113 sh long (+$2.8k), stop to be re-armed (Step 6).

## Fix (branch `regime-cluster-cap`)
- **`b72b5e9`** — sign-aware reconciler: a negative broker qty routes to
  `short_anomaly` (gate + surface, never flip/stop) at every reconciler entry point.
- **`2f0a460`** — point-of-sale long-only guard on the monitor composers
  (`exits` + `stop_ratchet`) + `reconcile.py:1859` sign-aware + `run_entry` message.
- Full test suite green (1920) at time of the fix.

## Remediation checklist (Step-5 runbook)
- [x] 1. Confirm flat (NFLX = 0, signed).
- [x] 2. Cancel resting NFLX orders (none open — no-op).
- [x] 3. Ledger `closed` WITHOUT `exit_price`; NFLX removed from positions.json;
      incident P&L recorded here (this file).
- [ ] 4. Verify residual-fix coverage + fill gaps + tests.
- [ ] 5. Dry-verify with crons still disabled.
- [ ] 6. Re-enable Reconcile; confirm GO stop re-armed.
- [ ] 7. Re-enable Monitor.
- [ ] 8. Clear the cron gate LAST; confirm entry unblocked.

Related: `project_nflx_runaway_short` memory; vault
`swing-2026-07-24-nflx-naked-short-guard-fix-handoff.md` +
`Output/2026-07-24-claude1-nflx-short-guard-fix-verify.md` (Alfred cross-check).
