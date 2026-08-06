# PRE-REGISTERED — fill-fidelity tolerance for the ~2026-08-13 ts_momentum_liquid_us cycle

**Date committed:** 2026-08-06 — BEFORE the next `ts_momentum_liquid_us`
rebalance fires (~2026-08-13). A tolerance written after the cycle is not a
tolerance (D3 requirement). This is the TRACKED mirror of the vault handoff
`Output/2026-08-06-claude1-fill-fidelity-tolerance-prereg.md`; the checks below
are verbatim from it. Scoring artifact to be produced after the cycle:
`ledgers/improvements/2026-08-XX-ts-momentum-fill-fidelity-cycle-1.md`.

**What this satisfies:** D3 deploy-trigger clause 2 — fill fidelity confirmed
over ≥1 full rebalance cycle within a tolerance stated in advance. The
certified assumption is an UNCONDITIONAL next-open fill; the live path
approximates it with a 09:35 DAY marketable limit at pivot × 1.03 (gaps R1 =
09:35-vs-open timing, R2 = the +3% cap skips gappers).

## The honest limit (stated up front, verbatim from the handoff)

One cycle produces at most 8 orders. **That cannot measure a fill RATE**
(resolution 12.5pp against an expected ~8% skip rate). This tolerance tests
the MECHANISM, order by order: every outcome must be explained by the model
with no unexplained residual. Anyone later citing this cycle as evidence about
the fill *rate* is misreading it — rate estimation needs accumulated cycles,
the same accumulation DSR needs (Handoff A).

## Task 0 — answered and instrumented (commit 5bf4516, before the cycle)

1. **Did a skipped order write a row?** NO — `execution_attribution` excluded
   unfilled ledgers entirely; all 11 historical drag rows are fills. A skip
   could not explain itself; R2 was retroactively unverifiable.
2. **Was the 09:35 price captured against `limit_price_placed`?** NO — an
   unfilled ledger retained pivot/limit/order_id but no market price of any
   kind.
3. **Instrumentation added (plumbing, no trials):** `compute_skip_rows` emits
   `row_type: "skip"` per unfilled entry ledger — placed limit, attempt-day
   cached OHLC, best-effort ~09:35 price (yfinance 5m, ~60-day window), and
   the deterministic resting-DAY-limit test `skip_explained = day_low > limit`
   — wired into the `update` CLI already called by `/auto-paper-reconcile`,
   append-only, idempotent. Known blind spot, declared now: a low printed
   between 09:30 and the 09:35 placement can false-flag a skip as unexplained
   by the day-low test; the ~09:35 intraday evidence (available within 60
   days) disambiguates, so cycle-1 skips will carry it.
   Backfill sanity: AMD/CAT/GOOGL (2026-06-15 pre-fix casualties) all
   EXPLAINED with intraday evidence; SO 2026-06-02 flags FAULT-unexplained —
   consistent with the broker-confirmed SO phantom already adjudicated by the
   2026-07-27 external review, not a new investigation.

## The tolerance — three checks (pre-registered, verbatim)

### C1 — Placement completeness (the one that matters most)
Every signal the strategy emits at rebalance must produce a placed order.
**Target 100%.** Any signal with no corresponding order attempt is a FAULT,
not a fill observation. (The track produced zero fills for nine weeks and
nobody noticed — before asking whether fills match, establish that orders are
being placed.)

### C2 — Skip explicability (R2)
Every non-fill must be explained by the stated rule: the 09:35 price exceeded
pivot × 1.03. **Target: 100% of non-fills explained, zero unexplained.** An
unexplained non-fill — order placed, price within the limit, no fill — is a
FAULT and is more serious than any number in C3.

### C3 — Timing cost (R1), measured not gated
Per filled order, `delay_cost_bps`. **Band: median |delay_cost_bps| ≤ 50**
across the cycle. Any single order > 200 bps is flagged for written
explanation, NOT an automatic failure (precedent: VRT 415 bps on 2026-05-26
was not a fault). Band basis: the historical fill log clusters ~29–58 bps
(SO 34.8, WMT 29.5, XOM 57.9, VAL −58.0) — set from the record, not theory.

## Outcome table — decided in advance

| Result | D3 status |
|---|---|
| C1 = 100%, C2 = 100%, C3 within band (or outliers explained in writing) | **FIDELITY CONFIRMED** — clause 2 satisfied for ts_momentum_liquid_us |
| C1 < 100%, or any unexplained non-fill | **FAULT** — clause 2 not satisfied; the fault is the finding; fix it and the next cycle becomes the eligible one |
| C3 median outside band, C1/C2 clean | **PARTIAL** — mechanism faithful, cost worse than modelled; record the figure, re-parameterise the cost model; clause 2 satisfied on fidelity with the cost delta disclosed |
| Rebalance emits zero signals | **VOID, not failed** — no cycle occurred; D3's clock keeps running |
| Signals emitted, zero orders placed | **FAULT — likeliest explanation of the nine-week drought; escalate immediately** |

## Scope and trials

- This is an operational reconciliation of a deployed strategy under its
  EXISTING fill model — **not a trial**; `trials.yml` untouched (stated here
  so it is not re-litigated).
- No entry-model change (LOO is DENIED on this account, both windows,
  2026-08-06; `top_k=8` stays per the cap audit). No new KINDs (freeze to
  2026-09-30).
- A FAULT here is a good outcome — the binding problem is that the automated
  track has produced nothing for nine weeks; finding out why, on a cycle that
  was going to run anyway, is worth more than a clean pass.
