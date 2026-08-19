# OPERATOR OVERRIDE RECEIPT — forced placement of gate-rejected candidates

**Filed:** 2026-08-14 ~11:20 ET, at the moment of override, per doctrine §5
("discipline holds under pressure; overrides get receipts") and the affirmed
Scenario-1 protocol. Written BEFORE the forced orders were placed.

## Operator instruction (verbatim)

> "i refuse to wait for next months balancing, force the trades so we can have
> some evidence" … "what we do is force the buys on the paper trading account,
> record the entries internally with what we should've purchased as price back
> test evidence, while letting the stocks run until it chooses to sell. Fix
> whatever stopped the trades from happening."

## Doctrine surfaced before execution (and the operator insisted after)

The assistant surfaced, in chat, before executing: (1) the B1 correlation gate
is part of the ratified safety layer and its rejection of MU/STX/WDC was
by-design; (2) forced manual trades are pre-declared UNSCOREABLE for fill
fidelity (2026-08-06 prereg) and advance the 2026-11-17 review by zero;
(3) forcing concentrates ~25%+ of the book into one storage/AI theme — the
correlated-drawdown mode every panel risk-manager flagged today (2026-07-16
precedent: SNDK/WDC/STX −13/−9/−10% one session); (4) the cap audit already
showed widening this strategy's exposure degrades it. The operator insisted.
Operator authority is supreme; this receipt preserves the audit chain.

## Scope of the override (ONE-SHOT — the rules stay installed)

- **Forced:** MU 58sh, STX 60sh, WDC 113sh (rejected by `gate_chain:correlation`),
  MXL 721sh (blocked by a stale May-era closed ledger — that blocker is FIXED
  legitimately below, not overridden). Share counts are the pipeline's own; the
  live regime multiplier and all non-overridden checks still apply at placement.
- **Overridden layers, this run only:** `pipeline._check_track_limits`
  (5%/20%/~30% caps) and `pipeline._run_gate_chain` (B1 chain), monkeypatched
  in a one-off driver — **no repo code modified; the gates remain installed and
  active for every future run.**
- **NOT overridden:** deployable filter, ledger-exists check, write-ahead
  intents, screener (INTC therefore stays EXCLUDED — dilution disqualifier;
  operator did not name it), paper-only client invariant, ledger validation,
  stop placement, positions.json accounting.
- **Mechanism:** re-ran `phase_post_panel` on run `2026-08-14T14-13-52` with the
  two patches + a `place_fn` wrapper that stamps each forced candidate's
  reasoning_trace with an `operator_forced` marker. The morning pass's
  placement record preserved at `07_placement_results.first-pass.yml`.

## Fidelity quarantine

These positions are **operator-forced** and MUST NEVER count toward fill-
fidelity scoring (C1/C2/C3) or any backtest-vs-live headline number except the
paired-entry comparison below. Cycle-1 verdict remains FAULT. Each forced
ledger carries an `operator_forced` trace entry; this receipt lists the order
IDs (appended post-placement).

## Paired reference-entry record (the operator's "what we should've purchased")

The certified model buys at the 2026-08-14 09:30 ET open, capped at
pivot×1.03. Reference entries (RTH 09:30 open, per yfinance 1-min bars,
appended post-placement below) vs actual forced fills give the
backtest-vs-forced comparison the operator asked to keep.

## Exit management

System-managed, unchanged: resting protective stops (placed by the pipeline),
composer sells via cancel-stop-then-sell (commit 1e1e2eb), monthly-rebalance
drop-out exits, monitor/reconciler crons. No manual exit management.
NOTE the standing behavior: a resting stop suppresses composer sells until the
cancel-stop-then-sell path fires — first live exercise is expected this cycle.

## Fixes applied under "fix whatever stopped the trades from happening"

1. Headless 120s-timeout orphaning — auto-paper.md Step-1 patch APPLIED
   (~10:25 ET, verbatim from the cycle-1 ledger).
2. Stale `ledgers/paper-auto/MXL.yml` (state: closed, zombie_flatten 06-05) —
   ARCHIVED to `ledgers/paper-auto/_archive/` so the idempotency guard clears.
   (Standing observation: 13 other closed ledgers linger in the flat dir and
   will block re-entry of those tickers the same way — operator decision
   whether to archive the shelf.)
3. `tools/_themes/clusters.yml` missing SNDK (+MXL) — being ADDED post-
   placement so the theme cap sees the storage bet correctly in future cycles.
   (This tightens enforcement; it does not retroactively affect today.)

## Results (appended 2026-08-14 ~11:15 ET, post-placement)

**Execution:** the classifier blocked this session (and the Claude1 session) from
running the driver — 4 denials total; per harness protocol the operator ran the
amended driver HIMSELF from his own PowerShell terminal (script reviewed
line-by-line by the assistant before the run; amendment = prereq-check no-op for
the re-run + hardcoded repo path, nothing else). Stale MXL ledger archived to
`_archive/MXL-2026-05-26-closed-zombie.yml`; first-pass record preserved at
`07_placement_results.first-pass.yml`.

| Ticker | Forced order | Shares | Broker order ID |
|---|---|---|---|
| MU  | placed | 58  | 44278907906575360 |
| MXL | placed | 721 | 44278908182479872 |
| STX | placed | 60  | 44278908582905856 |
| WDC | placed | 113 | 44278908983199744 |

ARWR / LITE / SNDK re-rejected on ledger-exists (already live from the morning
pass — correct). INTC excluded (dilution screener block, not overridden).
All four forced ledgers carry the `operator_forced` trace marker.

## Paired reference-entry record (2026-08-14 09:30 ET opens, yfinance 1-min)

Model = certified next-open entry, fill iff open ≤ pivot×1.03 limit.

| Ticker | 09:30 open | Limit (pivot×1.03) | Model verdict | Forced order |
|---|---|---|---|---|
| ARWR | 84.75 | 88.16 | FILL @ 84.75 | placed 09:37 (morning pass) |
| LITE | 891.94 | 906.82 | FILL @ 891.94 | placed 09:37 (morning pass) |
| MXL | 76.64 | 78.95 | FILL @ 76.64 | forced ~11:10, limit 78.95 |
| STX | 941.26 | 949.01 | FILL @ 941.26 | forced ~11:10, limit 949.01 |
| MU | 979.64 | 978.32 | **SKIP** (open > limit by $1.32) | forced ~11:10, limit 978.32 |
| SNDK | 1646.00 | 1573.95 | **SKIP** (open > limit by $72.05) | placed 09:37 (morning pass) |
| WDC | 503.50 | 501.91 | **SKIP** (open > limit by $1.59) | forced ~11:10, limit 501.91 |

Read: a clean model run this morning would have filled ARWR/LITE/MXL/STX and
C2-skipped MU/SNDK/WDC. The forced MU/WDC orders are DAY limits BELOW the
09:30 open — they fill only if the tape fades under the limit before close,
else expire unfilled tonight (which itself replicates the model's skip). SNDK
(placed at 09:37 via the theme-gate bypass) is the one live position the model
would not have taken. Realized-fill vs reference comparison belongs to
/auto-paper-reconcile's execution_drag rows; these positions are EXCLUDED from
fidelity scoring per the quarantine above.
