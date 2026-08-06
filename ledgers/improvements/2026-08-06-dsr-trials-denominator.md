# DSR trials denominator — what is N actually counting? (Handoff A)

**Date:** 2026-08-06
**Pre-registration:** `2026-08-06-dsr-trials-denominator-prereg.md` (commit `6858901`,
committed before any reading was cited or any number computed)
**Handoff:** `Output/2026-08-06-claude1-handoff-A-trials-denominator.md` (vault)
**Question:** `ledgers/trials.yml` carries one global counter (`n_trials_total: 91`)
and every strategy's DSR deflates against it. Correct denominator, or should N be
scoped to the search that selected the strategy?

**Verdict up front: the global counter is correct as the denominator — change
nothing about the count.** One implementation approximation is newly documented
(§2), one counting rule is adopted for future framework repairs (§4), and the
break-even N is disclosed at the end per prereg C3 (computed only after §1–§4
were committed).

---

## 1. What N means in the derivation (read from source)

**Read in full:** Bailey & López de Prado, *The Deflated Sharpe Ratio:
Correcting for Selection Bias, Backtest Overfitting and Non-Normality*, JPM
40(5) 2014 — the 14-page SSRN working-paper version (davidhbailey.com mirror),
every page read this session.
**Read partially:** Harvey, Liu & Zhu, *…and the Cross-Section of Expected
Returns*, RFS 29(1) 2016 — abstract, introduction, and §1 "The Search Process"
(journal pp. 5–8) read on-page; the Bonferroni/Holm/BHY machinery of §§2–4
skimmed via extraction, not read line-by-line. Claims below cite only the
on-page portions.

### 1.1 The definition of a trial (BLdP p. 7, "Expected Sharpe Ratios Under Multiple Trials")

> "More formally, consider a set of N independent backtests or track records
> associated with a particular strategy class (e.g., Discretionary Macro). Each
> element of the set is called a *trial*, and it is associated with a SR
> estimate, SR̂_n, with n = 1, …, N."

The trials are modeled as draws from one distribution: "the concept of
'strategy class' implies that the trials are bound by some common
characteristic pattern… we assume that there is a mean and variance associated
with the trials' {SR̂_n} for a given strategy class" (p. 7). Appendix A.1
derives E[max] for iid Normal draws (Eq. 3–6).

### 1.2 What N is FOR (BLdP p. 8, below Eq. 2)

> "V[{SR̂_n}] is the variance across the trials' estimated SR and N is the
> number of independent trials."
>
> "DSR deflates SR by taking into consideration five additional variables: …
> the variance of the SRs tested (V[{SR̂_n}]), as well as **the number of
> independent trials involved in the selection of the investment strategy
> (N)**."

And p. 9, discussing the Harvey–Liu threshold: "DSR computes how statistically
significant a particular SR̂ is, **considering the set of trials carried out so
far**."

Three load-bearing facts:

1. **N is the cardinality of the selection set** — the trials from which the
   deployed strategy was picked as the best. It is not a property of the
   winning strategy; it is a property of the search.
2. **N and V[{SR̂_n}] are defined over the SAME set** (Eq. 1 p. 7, restated in
   Eq. 2 p. 8). The worked example (pp. 9–10) makes the investor demand both
   numbers about the same 100 configurations.
3. **N means INDEPENDENT trials** (Appendix A.3, p. 14): "It is critical to
   understand that the N used to compute E[max{SR_n}] corresponds to the number
   of *independent* trials… Clearly, using M instead of N will overstate
   E[max{SR_n}]." The remedy the paper gives for dependent trials is Eq. 8–9:
   measure the average pairwise correlation ρ̂ and use N̂ = ρ̂ + (1−ρ̂)M —
   **shrink via measured correlation, not via truncating the family.**

Worked example (pp. 9–10), verified against our test fixtures: N=100,
V[{SR̂_n}]=1/2 annualized, T=1250, γ₃=−3, γ₄=10, annual SR 2.5 → SR₀ ≈ 0.1132
per-period, DSR ≈ 0.9004 < 0.95 → **rejected**; at N=46 DSR = 0.9505; with
Normal returns the same strategy survives to N=88. Note the example's 100
trials are all configurations **of one idea** (Treasury-auction seasonality) —
the within-idea reading of "strategy class."

### 1.3 The family scope (HLZ 2016, on-page)

- Abstract: "A new factor needs to clear a much higher hurdle, with a
  t-statistic greater than 3.0."
- Introduction (p. 5): "Given the known number of factors that have been tried
  and the reasonable assumption that **many more factors have been tried but
  did not make it to publication**, the usual cutoff levels for statistical
  significance may not be appropriate."
- §1 (p. 8): "perhaps most importantly, we should be measuring the number of
  factors tested (**which is unobservable**) — that is, we do not observe the
  factors that were tested but that failed to pass the usual significance
  levels and were never published."

HLZ's family is the **entire search history including unobserved failures** —
broader even than one researcher's program. Two of their counting practices
matter here:

- **Re-tests of an existing candidate do not increment the count** (§1 p. 7):
  "We do not include the hundreds of papers that test the CAPM in different
  contexts." M counts distinct candidate factors, not repeated measurements of
  one.
- **Correlated variants still count separately** (§1 p. 8 + fn. 5): they
  include four versions of idiosyncratic volatility "even though they are
  likely highly correlated" — dependence is handled in the adjustment, not by
  dropping trials from the count.

---

## 2. What `dsr_gate.py` actually implements — and whether it matches

**The math matches the paper exactly.** `tools/backtest/sharpe_stats.py`
implements Eq. 1 (`expected_max_sharpe`, mean 0 under H₀ per p. 8), Eq. 2
(`psr` / `deflated_sharpe`), and Eq. 9 (`effective_trials`). The paper's
worked example is pinned in `tests/test_sharpe_stats.py` (SR₀ 0.1132 / DSR
0.9004 / N=46 → 0.9505 / Normal N=88 → 0.9505) — this session's full read of
the paper confirms those fixtures reproduce pp. 9–10 verbatim. No
implementation bug was found; §2.2 of the handoff ("an implementation error is
more likely") is answered NO.

**One deliberate deviation from the derivation's letter, now documented:**
N and V are taken from **different trial sets**.

- `evaluate_grid` (dsr_gate.py:206): `n_trials = max(len(grid), registry_total())`
  → N is GLOBAL (91).
- `evaluate_grid` (dsr_gate.py:176-184) and `evaluate_roster_setup`
  (dsr_gate.py:304-313): `var_trials` = variance of the LOCAL spec grid's (or
  recorded sweep's) per-period Sharpes only.

The derivation defines both over the same {SR̂_n}. The hybrid exists because
`ledgers/trials.yml` records **counts, not Sharpes** — family-level V is not
computable from the registry as it stands. Direction of the two halves:

| Approximation | Direction | Status |
|---|---|---|
| Raw global N, no Eq. 9 correlation shrink | Overstates E[max SR] → deflates MORE | Conservative; already documented in the module docstring |
| Local grid V instead of same-set family V | Within-grid Sharpes cluster tightly; cross-family Sharpes span roughly −1 to +2 annualized, so family V would very likely be LARGER → SR₀ understated → deflates LESS | **Anti-conservative; not previously called out. It is now.** |

Net direction is unmeasurable today. Neither half is a bug; both are forced by
the registry schema. The principled refinement the source itself offers is to
record per-trial OOS Sharpes in registry components going forward, making BOTH
same-set V and Eq. 8–9 ρ̂ computable. **Paired-adoption constraint (prereg
C1/C2):** the Eq. 9 shrink (helps candidates) and same-set family V (hurts
candidates) are two halves of one correction to the same assumption — adopting
only the favorable half would be selection on the rule itself. Either both, or
neither. Neither is adopted this session (no re-evaluation in scope).

---

## 3. The answer

**The global counter is the correct denominator. Change nothing.**

1. **The selection event the gate prices is roster promotion, and roster
   promotion selects across the whole program.** The roster is "whatever
   passed, among everything tried" — `ts_momentum_liquid_us` is deployed
   *because* ~10 strategy families failed around it. Under BLdP's definition —
   N = "the number of independent trials involved in the selection" — the
   trials involved in selecting ts_momentum are all of them, not the 6 in its
   own spec family. Scoping N to the winning grid would hide the failed
   families from the count, which is precisely the file-drawer / selection-bias
   failure the paper opens with (pp. 3–4, including the ASA guideline: "Failure
   to disclose the full extent of tests… would be highly misleading").
2. **HLZ's family is broader still** — all tests ever attempted, including
   unobservable failures. Our registry note already encodes this: the total is
   a FLOOR because ad-hoc runs that left no JSON artifact are invisible. That
   floor is soft in practice (the insider KIND's six phases register as 1
   trial; the value-momentum breadth sweep, GEM overlay A/B, and the
   futures/options exploration left .md artifacts the deriver cannot see).
   Under the source literature the true hurdle is HIGHER than the current one,
   not lower. Any argument to shrink N fights an undercounted floor.
3. **The "strategy class" objection resolves against scoping.** It is true the
   91 pooled trials are not iid draws from one class — the derivation's
   common-distribution assumption is violated. But the paper's own remedy for
   heterogeneous, dependent trials is Appendix A.3: measure ρ̂ across trials
   and shrink to N̂ = ρ̂ + (1−ρ̂)M — with the paired family-V correction of §2.
   Truncating the family to the winning class is not a remedy the source
   offers anywhere. HLZ likewise keep correlated variants in the count and
   handle dependence in the adjustment.
4. **Per prereg C2:** none of the above depends on what any rule does to
   ts_momentum, and its passing under any future refinement would be an
   outcome, not a confirmation. For the record, the direction cuts the other
   way: with a soft floor and an anti-conservative local-V half, ts_momentum's
   true DSR is, if anything, LOWER than the recorded 0.629.

## 4. The repair-vs-search rule (ADOPTED)

The handoff §3 concern is real: without a rule, uniformly re-measuring the
roster after a harness bug fix would register ~20 new trials, raising the bar
for everything as the price of correctness. The candidate bright line is
adopted **with a third clause added**:

> A framework/harness change counts as **ZERO new trials** iff ALL of:
> **(a) Uniform application** — it is applied to every strategy, passed and
> failed alike;
> **(b) Full publication** — the complete re-evaluated roster is published,
> survivors and casualties, not only the names that improved;
> **(c) Pre-committed and irreversible-by-results** — the change is adopted on
> its own correctness justification, committed BEFORE any of its re-evaluated
> results are seen, and cannot be rolled back in response to those results. No
> regime-shopping between old and new harnesses.
>
> Anything that fails a clause is a search and is priced as one. In
> particular: re-measuring only a subset you hope will pass (fails a); any NEW
> parameter value, universe, or signal variant evaluated during a "repair"
> (that is a new candidate, not a re-measurement — always a trial); choosing
> between measurement regimes after seeing both (fails c).

**Source grounding.** BLdP's trials are candidate configurations in the
selection set — a uniformly-applied measurement correction adds no candidates,
so N is unchanged. HLZ's own counting practice is explicit that re-tests of an
existing candidate do not increment M (the CAPM-papers exclusion, §1 p. 7);
only distinct candidates do. Clause (c) is the project's addition: it closes
the meta-selection hole where "keep old vs adopt new harness" becomes itself a
2-way selection conditioned on outcomes — the exact abuse the handoff warned
the rule invites.

**Reconciliation with the 2026-08-06 doctrine-review norm** ("any performance
statistic computed for a retired KIND under a new fill model is a trial"):
consistent — that norm describes *exploratory* re-measurement under a regime
chosen with results in view and applied selectively, which fails clauses (a)
and (c). Both rules are the same principle observed from opposite sides.

**Registry effect:** the rule is recorded in the `write_registry` note string
in `dsr_gate.py` (the durable source — `derive --write` regenerates
`trials.yml`'s note from it) and mirrored into `ledgers/trials.yml` now.
Counts unchanged: `n_trials_total: 91`, all components preserved.

**Symmetry obligation (prereg C1):** this rule creates no count change today,
so no re-evaluation is triggered. If a future uniform repair (e.g. Handoff B's
cap audit) re-measures the roster under this rule, the FULL roster's results
must be published — including every retired strategy that stays retired.

---

## 5. Disclosure — break-even N for ts_momentum

*Computed only after §§1–4 were committed (`dd8b2b5`, prereg C3). This section
was intentionally absent from the §4 commit.*

Method: identical inputs to the recorded 2026-07-24 roster evaluation
(`2026-07-24-statistical-gates-batch-c.md` — net OOS Sharpe 1.23 ann, T=2359,
skew +0.41, raw kurtosis 10.74; V[{SR̂}] rebuilt from the two per-combo
per-period Sharpes in `2026-07-02-ts_momentum-lookback-sweep.json`, sample
variance 8.18e-4), then N varied. Reconstruction check: N=83 → DSR 0.6338 /
SR₀ 1.119 ann vs the recorded 0.629 / 1.12 (drift = rounding of the recorded
moments).

| N | DSR | verdict |
|---|---|---|
| 9 | 0.9520 | pass |
| **10** | **0.9439** | **fail — break-even is between 9 and 10** |
| 83 (recorded) | 0.6338 | fail |
| 91 (current floor) | 0.6163 | fail |

**Reading, stated against interest:** `ts_momentum_liquid_us` clears DSR > 0.95
only if the project had run **nine or fewer trials in its entire history**. No
defensible count gets there — EXCEPT one: scoping N to the ts_momentum family
alone (2 + 2 spec-grid combos + 2 sweep combos = 6) would have produced a PASS.
That is precisely the scoping §3 rejected on the sources, and it is why the
prereg ordering mattered: the one denominator that flips the verdict is the one
the derivation does not permit. The gap between 10 and 91 is not closable by
any honest refinement — Eq. 9's correlation shrink would need the 91 trials'
average pairwise ρ̂ ≈ 0.9, implausible across ~10 unrelated strategy families,
and the paired same-set family-V correction (§2) pushes the other way.

Caveat inherited from the recorded evaluation: V comes from only 2 same-setup
trials (per-period 0.0368 vs 0.0772) — a very noisy variance estimate. The
break-even N moves with V; recording per-trial Sharpes going forward (§2)
firms this up. It does not plausibly move the conclusion across the 10-vs-91
gap.

**Standing consequence:** the sole live deployable remains, on the recorded
evidence, not yet statistically distinguishable from selection luck at the 95%
bar. That is what the 2026-07-24 artifact already said; this review confirms
the denominator behind it is the correct one. The path to a passing DSR is
live/paper evidence accumulating T on the deployed combo — not a smaller N.
