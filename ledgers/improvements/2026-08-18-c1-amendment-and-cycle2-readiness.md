# C1 amendment + cycle-2 readiness (handoff 2026-08-17, executed 2026-08-18)

**Handoff:** `Output/2026-08-17-claude1-handoff-c1-fix-and-cycle2-readiness.md`.
Three scored cycles remain (placements 09-15, 10-14, 11-12) before the
2026-11-17 D3 review; cycle 1 is spent on FAULT. Everything below landed
before the 09-15 placement. **Not a trial — `trials.yml` untouched at 103.**
No entry-model change, no cap change, no `hold:` removals, nothing un-retired,
no thematic work.

---

## 1. C1 → C1a/C1b amendment (committed `ab3002e`, own commit) + mandatory re-score

Appended to `ledgers/improvements/2026-08-06-fill-fidelity-tolerance-prereg.md`
(original C1 text preserved verbatim; C2, C3, and every other outcome-table
branch untouched):

- **C1a — pipeline completeness (gated, target 100%):** every emitted signal
  (`00_signals.yml` selected set) must reach a terminal, attributable
  disposition — an order attempt, OR a named gate decline with the gate
  identified and its rule cited. A signal reaching neither (vanished, silent
  exit, orphaned process, unlogged drop) is a FAULT.
- **C1b — gate-decline census (recorded, NOT gated):** every decline named
  with its gate and rule, reported every cycle.

Justification is definitional, not threshold relief: the attended 08-14 re-run
proved a fully healthy pipeline scores 3/8 = FAULT under the original text
(ARWR/LITE/SNDK placed; MU/STX/WDC `gate_chain:correlation`; INTC dilution
screener; MXL ledger-exists guard) — D3 clause 2 was unsatisfiable by any
gated pipeline. Committed per repair-vs-search clause (c): own correctness
justification, before any cycle-2 result, irreversible-by-results.

**Mandatory falsification test — cycle 1 re-scored under C1a: STILL FAULT.**
The scored run is the 09:35 headless attempt `2026-08-14T13-35-11`:
`00_signals.yml` = 8 selected; `_status.yml` `phases_completed: []`; no
screener record, no placement results, no order ledgers, no gate declines —
the orphaned process died before writing any disposition. **C1a = 0/8
terminal dispositions → FAULT.** The amendment does not change cycle 1's
verdict; the case it re-scores is exactly the orphaned-process case C1a
names. (Had it passed, the amendment would have been rejected as
accommodation per the handoff's own test.)

---

## 2. Stale closed ledgers: verdict = **BUG**, with the code path (fixed `a63e719`)

**Code path:** `tools/auto_paper/pipeline.py` step 2 of `place_candidate`
guarded with `state.ledger_exists(ticker)` → `tools/auto_paper/state.py:42`,
a bare `os.path.isfile(ledgers/paper-auto/<TICKER>.yml)`. **State-blind**: a
`closed` ledger satisfied it exactly like an open position, and nothing in
the lifecycle (reconcile close, composer exit, rebalance drop-out) ever moved
a closed ledger out of the flat dir. The in-code comment says "don't
double-up" — a guard against concurrent duplicate positions, not a cooldown;
no written rule anywhere specifies one-trade-per-ticker-ever. For a monthly
rebalance that legitimately re-selects names, that is a bug, and it was
demonstrated (MXL, cycle 1). At audit time **19 of the 20 flat-dir ledgers
were terminal** (only ARWR open) — every previously-traded ticker was
pre-loaded to produce the same teaches-nothing decline on 09-15, and
momentum re-selects prior names by construction.

**Fix (rule, not just files):**
- OPEN states still hard-block, now named for the C1b census:
  `double_entry_guard: open paper-auto ledger already exists … (state=…)`.
- TERMINAL states (`closed` / `closed_unfilled`): archived to
  `ledgers/paper-auto/_archive/<TICKER>-<close-date>-<state>.yml` (moved,
  never deleted) and placement proceeds. Dry runs defer the move.
- **`reentry_cooldown`** (new named decline, C1b): re-entry within 7 calendar
  days of a FILLED close is declined — conservative enforcement of the
  CLAUDE.md §Discipline no-revenge-trading rule (5 trading days), which the
  old block-forever behavior had enforced vacuously and the naive fix would
  have silently dropped. Unfilled closes carry no cooldown (no trade
  occurred).
- **Scanners follow the files:** realized-performance enumeration
  (`performance.py`), the live sleeve curve (`live_vs_backtest.py`), and
  execution-drag fill/skip rows (`execution_attribution.py`) all include
  `_archive/` — otherwise archiving would re-create the 2026-07-26
  enumeration bug (realized history silently dropped; the 08-14 MXL archive
  had already put its ledger outside the old scanners).
- **One-time shelf clear:** all 19 terminal flat-dir ledgers archived via the
  new production function; realized stats verified identical before/after
  (n_realized 12 — including the five August round trips MU/STX/WDC/LITE/MXL
  now served from `_archive/`). Flat dir = ARWR only, the actual book.

Tests: 9 new (guard branches, cooldown, dry-run non-mutation, archive
function, scanner `_archive/` inclusion). Full suite **2142 passed**.

---

## 3. DSR-T: the gate evaluates a **frozen backtest curve** — clause 1 cannot fire by 2026-11-17

`dsr_gate.evaluate_roster_setup` (`tools/backtest/dsr_gate.py:279`) re-runs
the deployed combo through the net-of-cost walk-forward on cached data and
takes T from that curve's daily returns. The window is
`spec["period"]["start"]/["end"]` (`scripts/volume_share_slippage_rerun.py:52-53`)
— for `ts_momentum_liquid_us` **pinned at 2017-01-01 → 2026-05-25** in the
spec file. Live fills appear nowhere in any DSR path (no reference to
`journal/paper-auto`, ledgers, or the broker; `grep dsr tools/auto_paper/` =
zero matches). Live evidence feeds the **separate** B3 PSR
(`live_vs_backtest`), never DSR.

**Consequence, stated plainly:** D3 clause 1 (DSR ≥ 0.95) **cannot fire by
2026-11-17.** The number is frozen at 0.593 (N=103) no matter what the three
remaining cycles do. And the gap is not marginal — on the recorded moments
(SR 1.23 ann, T=2359, skew 0.41, kurt 10.74, V=8.18e-4; reconstruction
matches the recorded 0.593):

| What would move DSR to ≥ 0.95 at N=103 | Required | Actual |
|---|---|---|
| Higher Sharpe at frozen T=2359 | **≈ 1.69 ann** | 1.23 |
| Longer T at Sharpe 1.23 | **≈ 115,000 obs (~456 years)** | 2,359 |
| Re-pin spec end to Nov 2026 (+63 obs) | — | DSR 0.5943 (moves nothing) |

So the three remaining cycles are **execution validation for a future
deployment, not a deploy trigger** — the November review's clause-1 outcome
is already knowable in August. Worth saying against interest: even the
08-06 artifact's "path = live T" framing understates the gap; at the
recorded Sharpe no realistic T reaches 0.95. Clause 1 fires only on a
materially better realized record (which the accumulating live/paper curve
would evidence via PSR, on the operator's timeline, not this one).

---

## 4. 09-15 readiness — one line

With the timeout patch applied and the stale-ledger blocker fixed, the only
remaining non-execution FAULT path is a mid-run session kill between
`00_signals.yml` and the disposition writes (machine restart / power /
harness kill) — the Windows Update pause (operator-handled, runs to
**2026-09-21**) covers 09-15 but **expires before the 10-14 and 11-12
placements**, where a TrustedInstaller restart at 09:35 (the mechanism that
destroyed the 08-13 reconcile) would reproduce cycle-1's FAULT exactly.

Gate declines (correlation, screener, cooldown, double-entry) are C1b census
entries on 09-15, not FAULTs. Zero signals = VOID, clock keeps running.
