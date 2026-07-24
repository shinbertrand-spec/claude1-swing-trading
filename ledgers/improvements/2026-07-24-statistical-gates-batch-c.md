# Statistical gate upgrade — cherry-pick Batch C (C1–C2)

- asof: 2026-07-24
- implemented by: operator-carried cherry-pick session (vault spec `Output/2026-07-24-claude1-cherrypick-implementation-spec.md`, Batch C)
- branch: `cherrypick-batch-bc`
- dependencies added: **none**. License discipline per spec: DSR re-implemented
  from the paper's own formulas (AGPL pypbo NOT used even as reference reading
  beyond its stated numbers; all-rights-reserved mlfinlab NOT read); CPCV
  splitter semantics from MIT sam31415/timeseriescv (verified 2026-07-24),
  code re-written.

## C1 — Deflated Sharpe 7th gate

Implementation: `tools/backtest/sharpe_stats.py` (PSR/E[maxSR]/DSR/effective-
trials; stdlib-only — `math.erf` CDF + Acklam-with-Halley inverse CDF) +
`tools/backtest/dsr_gate.py` (trial registry, grid evaluation, roster CLI) +
wiring in `quant_strategies/runner.run_spec` (DSR reported on every grid run;
`gate.dsr_min` opts a spec into hard enforcement; an enforced failure vetoes
the selected combo and is surfaced, never auto-retired).

**Unit-test anchor = the paper's published worked example** (Bailey & López de
Prado 2014, pp. 9–10; PDF fetched + read 2026-07-24): SR0 ≈ 0.1132
non-annualized at N=100/V=1/(2·250); DSR ≈ 0.9004 < 0.95 (reject); N=46 →
0.9505; Normal-returns variant → 0.9505 at N=88. All pinned in
`tests/test_sharpe_stats.py` (abs tol 5e-4).

**Trial-count derivation** (`ledgers/trials.yml`, reproducible via
`python -m tools.backtest.dsr_gate derive --write`): one trial per param combo
per strategy spec grid (19 specs) + one per recorded sweep-artifact element
(2 sweeps) = **N = 83**. This is a FLOOR — ad-hoc unrecorded runs are
invisible, and a floor UNDER-deflates. CLAUDE.md now requires every new sweep
to append to the registry.

**Roster result (the headline finding):**

| setup | net OOS Sharpe (ann) | T (days) | skew | kurt (raw) | N trials | SR0 (ann) | DSR | gate |
|---|---|---|---|---|---|---|---|---|
| ts_momentum_liquid_us (LIVE) | 1.23 | 2359 | +0.41 | 10.74 | 83 | 1.12 | **0.629** | **FAIL < 0.95 — surfaced** |

Reading, stated carefully:

- After correcting for 83 recorded trials and the fat-tailed return
  distribution (raw kurtosis 10.7), the probability that the live setup's true
  Sharpe exceeds the best-of-83-skill-less-trials ceiling (SR0 = 1.12 ann) is
  only **62.9%** — far from the 95% evidence bar. The only live setup is not
  yet statistically distinguishable from selection luck at that confidence.
- Caveats cut BOTH ways: (a) V[{SR}] here comes from only 2 recorded
  same-setup trials (lookback 126 vs 252) — a very noisy variance estimate;
  (b) using the family-wide N=83 with that same-setup variance is, if
  anything, GENEROUS — the family-wide trial variance (setup Sharpes ranged
  −3..+2) is larger and would deflate harder.
- What this does NOT mean: retire the setup. Per spec, DSR failures surface to
  the operator. The correct response is already built: the live paper sleeve
  (B3) accumulates true out-of-sample evidence that no amount of backtest
  re-analysis can — PSR-vs-benchmark firms up as live T grows. It DOES mean:
  treat live sizing as validation-grade, and treat any future roster promotion
  without a DSR pass as unproven by policy.

## C2 — CPCV path-distribution gating

Implementation: `tools/backtest/cpcv.py` — `CombinatorialPurgedCV` (contiguous
groups, C(N,k) test combinations, purge-by-containment + optional embargo),
`path_assignment` (each group's j-th occurrence → path j; every path covers
every group exactly once — asserted in tests), and `evaluate_setup_cpcv`
(per-(param, group) segments precomputed once — 66 combinations then cost
array ops; selection = best train-groups pooled Sharpe per combination;
leakage controlled by SEGMENT TRUNCATION: each group simulated standalone,
positions force-close at the boundary, so no label ever crosses a
train/test edge). 12 splitter tests incl. the synthetic no-leakage property
test (`tests/test_cpcv.py`).

Honest note kept from the research: with daily non-overlapping labels the
purge/embargo add little — the path DISTRIBUTION is the value; embargo stays
small.

Roster run: `journal/backtest/2026-07-24-cpcv-ts-momentum.md`
(N=12 groups × k=2 → 66 combinations → 11 recombined OOS paths over
2017–2025). Headline numbers: path Sharpe min/p5/median all 1.07, maxDD
−13.7% — **but the distribution is DEGENERATE**: the 2-point param grid is
dominated by lookback=252 in all 66 train subsets, so all 11 paths are the
same path. Legitimate findings: end-to-end machinery verified; param
selection perfectly stable across every 10-of-12 training subset; the
segment-truncation haircut is visible (path 1.07 vs continuous walk-forward
1.23). Making C2 informative needs a wider selection grid — an OPERATOR
decision, because each added grid point is a new trial that deflates C1's
DSR further (the two gates deliberately price variant-mining). The proposed
5th-percentile > 0.0 threshold stands as a proposal, not applied.

## Follow-ups (operator)

1. Decide the C2 gate threshold (proposal: 5th-percentile path Sharpe > 0.0)
   and whether C1's DSR<0.95 finding changes the live-validation sizing plan.
2. When the next sweep runs anywhere in the family:
   `python -m tools.backtest.dsr_gate derive --write` (registry is a floor).
3. Optional depth: measure average signal correlation between trials and use
   `sharpe_stats.effective_trials` (paper Eq. 9) instead of raw N — raw N is
   the conservative default.
