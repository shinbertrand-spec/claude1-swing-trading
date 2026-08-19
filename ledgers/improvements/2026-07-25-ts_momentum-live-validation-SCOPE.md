# ts_momentum live-validation sleeve — SCOPE (2026-07-25)

**This is a plan for LIVE capital. It authorizes nothing by itself.** Every
real-money step below requires explicit, separate operator go. No live orders,
no `allow_live` flip, no account setup happen from this document.

## STATUS RECONCILIATION (added 2026-07-25 — read first)

Branch scoping revealed the live-validation effort is FURTHER ALONG than this
scope assumed. On `regime-cluster-cap` (which **already strictly contains**
`cherrypick-batch-bc` + the NFLX guards — no merge needed), Alfred/operator have:
- **Pre-condition #1 (branch integration): DONE** — `regime-cluster-cap` is the
  integrated branch (merge `170c596`).
- **Pre-condition #2 (gate_chain wired): DONE + TESTED** — `c9b17c7` wired
  `gate_chain.run_chain` into `pipeline.place_candidate` (step 4c-ter, fail-closed,
  per-candidate JSONL log, breaker persists on real runs, Kelly priors from the
  roster row). `tests/test_auto_paper_pipeline_gate_chain.py` present; suite 2074 green.
- **Roster sprint already run** (`2026-07-24-phase1-live-validation-finishup.md`,
  0/3 — connors DSR 0.846 / xs_lowvol 0.341 / insider n/a) — this session's
  independent roster sprint **reproduced it** (matching DSRs), so it's corroborated.
- **Their authoritative plan:** `plans/2026-07-24-second-stream-diversification-plan.md`
  — and their diversification answer is a **Phase-2 options-VRP second stream in a
  separate venture (claude2)**, NOT cross-asset/shorts. This session's cross-asset +
  shorts exploration is negative confirmation that the equity-trend family is mined out.

### THE REAL BLOCKER — ts_momentum has ZERO fills

The honest headline Alfred surfaced: **ts_momentum_liquid_us has never filled a
single order.** All three June PAPER entries (AMD, CAT, GOOGL) expired UNFILLED
(DAY limit orders expired). Its "validation clock has not started." This is the
critical-path item before ANY live capital: **a live sleeve that doesn't fill
validates nothing, and it's the same failure mode paper or live.** Diagnose + fix
the entry-fill mechanism (marketable-limit price/TIF on the monthly rebalance)
FIRST. Operator was watching for the first real fill on 2026-07-25.

**Net effect on this scope:** pre-conditions #1-#2 below are satisfied; the new
top priority is the FILL fix (call it pre-condition #0). Sizing decision = 
backtest-fidelity risk sizing (operator, 2026-07-25). Validation window = 3-mo /
20-trade EARLY READ (operator) — an execution checkpoint, not an edge verdict.

## Why — the one lever left

ts_momentum_liquid_us is the sole surviving edge. It is **paper-validated**
(net-of-cost OOS Sharpe ~1.04) but has **never been proven live** — the single
live-ish run was the 10-day silent-failure outage that produced no trades. Every
other lever this month came up empty (roster 0/3; cross-asset diversifier fails
the marginal gate under both weight rules; shorts don't pay). An unproven *sole*
engine is the real fragility — more than "only one strategy." **One edge proven
live > many unproven in backtest.** This sleeve converts the one edge from
"clears the backtest gate" to "makes money with real fills, slippage, and
operational reality."

## What — the sleeve

- **$10,000**, ring-fenced in a **separate Tiger LIVE sub-account**, isolated so
  a sleeve bug cannot touch the main discretionary holdings.
- Runs **ts_momentum_liquid_us ONLY** — deployed params `lookback_days=252,
  top_k=8`, monthly rebalance, ATR stop (paper-auto ATR carve-out), **long-only**.
- **Read-only** monitoring of the main discretionary book — surfaced, never acted on.

## The gravity — this is a doctrine change

Everything in this stack to date is **paper** (`TigerClient` refuses live by
construction: `allow_live=False` default + a fail-safe raise on any genuinely-live
order). Going live flips that for one ring-fenced sub-account — **the single most
dangerous change in the codebase.** The NFLX incident (a paper bug that
manufactured a −29,760 short and a $129k artifact) is the cautionary tale for what
an unguarded live bug does with real money. **Every guard must be armed before $1
of live capital.**

## Pre-conditions — ALL must hold before any live order

1. **Branch integration.** The NFLX long-only guards (`short_anomaly`,
   point-of-sale guard, sign-aware reconciler, `cron_gate`) live on
   `regime-cluster-cap`; the `gate_chain` circuit breaker + `live_vs_backtest`
   validation tool live on `cherrypick-batch-bc`. Live validation needs **both**,
   merged into one deployable branch. Today they are split.
2. **gate_chain wired into the live order path.** The B1 chain (0.5×-Kelly sizing,
   5%-ADV liquidity, correlation/theme, concentration, −20% sleeve circuit
   breaker) exists but its `pipeline.place_candidate` wiring was **deferred pending
   NFLX Step-5** — which is now resolved, so this is unblocked. Must be wired +
   tested before live.
3. **Long-only guards active** (verified by the NFLX regression suite, 1926 tests).
4. **Circuit breaker armed** — `gate_circuit_breaker` trips at −20% from sleeve
   high-water to no-new-entries until a logged operator reset.
5. **cron_gate / kill path operational** — a single command halts all sleeve crons.
6. **Sub-account isolation confirmed** — a bug in the sleeve provably cannot reach
   the main holdings (separate account, separate credentials, separate positions.json).

## Phased rollout — each phase gated by explicit operator go

- **Phase 0 — paper dress rehearsal on the EXACT live code path.** Run the full
  entry → monitor → reconcile loop with `allow_live` STILL False, against the
  paper account, for ~4 weeks (≥1 full monthly rebalance). Confirm zero anomalies,
  gate_chain logging clean, reconciler sign-correct, breaker/high-water tracking
  sane. Nothing new is trusted until it survives this on the real path.
- **Phase 1 — live, minimal.** Flip `allow_live=True` for the ring-fenced
  sub-account ONLY. Deploy a fraction (e.g. 1–2 positions, ~$2k). **Operator
  present for the first live fills.** Verify fills, stops, reconciliation against
  the live broker for one rebalance cycle.
- **Phase 2 — live, full sleeve.** Scale to the full $10k / top_k=8 once Phase 1 is
  clean.
- **Phase 3 — validation window.** Run for the pre-specified duration; evaluate.

## Sizing — the small-sleeve cap tension (open decision)

At $10k with `top_k=8`, equal-weight ≈ $1,060/position ≈ **10.6% each**, which
exceeds the **5% per-position cap**. Options:
- (a) **Size to backtest fidelity** (`risk_per_trade=0.01` + ATR stop, as the
  backtest did) so live results are comparable — the $10k ring-fence itself is the
  concentration control, per the paper-auto ATR carve-out logic. *(recommended —
  fidelity is the whole point of a validation sleeve)*
- (b) reduce `top_k` (e.g. 4–5) to respect the 5% cap, but then it's not the
  validated config.
- (c) apply the 5% cap to sleeve equity → ~40% max deployed, chronically
  under-invested → live Sharpe not comparable to backtest.
Liquidity is a non-issue at this size (5%-ADV gate never binds on ~$1k orders in
the liquid_us universe).

## Success / failure criteria — pre-specified

- **Primary metric:** `live_vs_backtest` **PSR** = P(true live Sharpe > backtest
  benchmark), benchmarked against ts_momentum's **net-of-cost** OOS aggregate
  Sharpe (the roster's `live_benchmark_sharpe`, NEVER the zero-cost figure).
- **Minimum duration:** ≥ 6 months AND ≥ ~30 closed trades — small-T PSR is weak
  evidence by design; the report says so on its face. No verdict before then.
- **Attribution:** `execution_attribution` (delay cost + execution residual) must
  stay within the backtest's cost assumptions; monthly drag > 25 bps flags.
- **Circuit breaker:** −20% from sleeve high-water → auto-halt + operator review.
- **PASS** = live PSR credibly ≥ backtest benchmark over the window with drag
  within model. **FAIL** = live Sharpe materially below backtest (execution drag,
  slippage, or alpha decay) → diagnose or retire; do not add capital to a failing
  live sleeve.

## Read-only monitoring of main holdings

Separate, non-trading: a scheduled snapshot of the discretionary book (positions,
concentration vs hard rules, regime) surfaced to Telegram — **observe only**, no
automated action. Reuses the `portfolio-manager` snapshot + the cockpit
health-check pattern. This is decoupled from the sleeve and carries no live-order
risk.

## What this scope does NOT authorize

No live orders. No `allow_live=True` flip. No sub-account setup. No code merges.
No capital movement. Each of those is a separate, explicitly-authorized step. This
document is the plan and the safety contract, nothing more.

## Open decisions for the operator

1. **Broker mechanics:** does the Tiger account support a genuinely-isolated live
   sub-account, or is it a separate login/credentials set? (verify before Phase 1)
2. **Branch integration:** merge `regime-cluster-cap` (NFLX guards) and
   `cherrypick-batch-bc` (gate_chain + live_vs_backtest) — merge order + who reviews.
3. **Sizing rule:** (a) backtest-fidelity risk sizing vs the 5% cap (recommended a).
4. **Validation window length + PASS threshold** (proposed ≥6 mo / ≥30 trades,
   PSR credibly ≥ net-cost benchmark).
5. **First-fill supervision:** operator present for Phase 1 live fills.
6. **Capital source & timing** for the $10k.
