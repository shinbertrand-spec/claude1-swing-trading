# PRE-REGISTRATION — DSR trials-denominator review (Handoff A)

**Date:** 2026-08-06
**Status:** PRE-REGISTERED — committed BEFORE any source reading is cited, before
the implementation check is written up, and before ANY number is computed.
**Handoff:** `Output/2026-08-06-claude1-handoff-A-trials-denominator.md` (vault)
**Deliverable it binds:** `ledgers/improvements/2026-08-06-dsr-trials-denominator.md`

---

## Question under review

`ledgers/trials.yml` carries one global counter (`n_trials_total: 91`) and every
strategy's DSR is deflated against it. Is one global counter the correct
denominator, or should the count be scoped to the search that actually selected
the strategy?

Context that makes this dangerous: `ts_momentum_liquid_us` — the sole live
deployable — passes its composite gate and **fails DSR at 0.629**. Any answer
that shrinks the denominator is an answer that could flip the only strategy the
operator cares about from FAIL to PASS. "The bar is too high" is exactly what a
p-hacker says. This pre-registration exists to bind the review before that
gravity acts on it.

## Binding constraints (in force before any computation)

### C1 — The symmetry constraint

Whatever counting rule this review reaches applies **retroactively and in both
directions**:

- If the effective count for a strategy's DSR **drops**, then **every
  previously-failed strategy is re-evaluated under the same rule** — not only
  the one that would benefit. The re-evaluation itself is separate work (out of
  scope this session per handoff §6), but the obligation attaches the moment
  the rule is adopted, and the full re-evaluated roster must be published —
  survivors and casualties alike.
- If the effective count **rises** (e.g. the review finds the registry floor is
  understated), the higher bar applies to every strategy too, including any
  currently passing.
- A rule applied to winners and not losers is a search wearing a rule's clothes.

### C2 — ts_momentum passing is not evidence

If the adopted rule results in `ts_momentum_liquid_us` clearing DSR, **that is
an outcome, not a confirmation of the rule**. The rule's justification must
stand entirely on (a) what N means in the Bailey & López de Prado (2014)
derivation, read from source, and (b) what `tools/backtest/dsr_gate.py`
actually implements. The deliverable must state this explicitly next to any
changed verdict.

### C3 — Order of operations

The break-even N for `ts_momentum` (the count at which it would pass) is
**motivated reasoning in numeric form** and will NOT be computed until the §4
answer — including "the global counter is correct, change nothing" as a live
option — is written and committed. It is then computed once, as a disclosure,
never as an input.

### C4 — Method rules (handoff §2, restated as binding)

1. Answer from the source: Bailey & López de Prado (2014) on what N represents
   *in the derivation*; Harvey, Liu & Zhu (2016) on multiple testing — not what
   N "plausibly ought to mean." The deliverable states what was read in full vs
   skimmed.
2. Implementation before philosophy: read `dsr_gate.py` / `sharpe_stats.py` and
   establish what the code does with N and the variance-of-trials term. An
   implementation error is more likely than a conceptual one; if the answer is
   a bug, the philosophical question may not need answering.

### C5 — Candidate bright line (recorded, NOT adopted)

The handoff §3 proposes one candidate repair-vs-search rule, recorded here so
that adopting it later is a decision against pre-registered text, not a drift:

> A framework change counts as **zero** new trials iff (a) it is applied
> uniformly to every strategy, passed and failed alike, and (b) the full
> re-evaluated roster is published, not only the survivors. Re-running only the
> strategies you hope will pass IS a search, and is priced as one.

This candidate is to be evaluated against the source literature. It is not
adopted by virtue of appearing in this file. "Propose better if better exists."

### C6 — Scope boundary (handoff §6)

- No re-evaluation of any strategy this session — the rule first.
- No deployment, no `hold: true` removals, no un-retiring, no new KINDs
  (candidate freeze D1 in force).
- `trials.yml` header/derive rule may be amended only if the §4 verdict
  concludes it should change, with old counts preserved, never overwritten.
