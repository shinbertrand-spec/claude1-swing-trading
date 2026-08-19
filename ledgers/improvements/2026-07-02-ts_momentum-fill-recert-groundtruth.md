# ts_momentum_liquid_us — fill-model re-certification v2 (2026-06-20)

Kind `ts_momentum`, lookback=252, top_k=8, period 2017-01-01..2026-05-25.
Gate: Sharpe>1.0 ∧ |MDD|<25.0% ∧ n≥30 on FULL ∧ rolling-OOS-aggregate, AND ≥50% of rolling OOS windows clear Sharpe>0.5.

Realistic execution model (LIVE row = what the scanner places): momentum = DAY marketable limit prior_close×1.03, fills at next-bar OPEN iff open≤limit; **FULL effective spread charged on the marketable cross** (FIX 2); market-type exits (stop/gap/max-hold) also cross full spread. Same signals + cost model across all rows; only fill SELECTION / stress differs.

| variant | scope | Sharpe | \|MDD\|% | n | fill% | gate |
|---|---|---|---|---|---|---|
| LIVE mkt-limit 3% (full-spread) | FULL | 1.35 | 16.9 | 736 | 92 | PASS |
| LIVE mkt-limit 3% (full-spread) | OOS-agg | 1.23 | 16.9 | 525 | 91 | PASS |
| STRESS (+5bps slip, 85% fill) | OOS-agg | 1.26 | 15.2 | 450 | 78 | PASS |
| pure_moo (diagnostic, not live) | FULL | 1.27 | 17.5 | 802 | 100 | PASS |

## Per-window OOS robustness (FIX 1 — the clause v1 skipped)
- LIVE: 3/6 windows clear Sharpe>0.5 (pass rate 50%) → **clause PASS**
  - windows: 2020:0.64✓(n86) 2021:0.54✓(n81) 2022:0.29✗(n92) 2023:0.41✗(n95) 2024:0.63✓(n86) 2025:0.50✗(n85)
- STRESS: 4/6 clear (pass rate 67%) → **clause PASS**
  - windows: 2020:0.59✓(n75) 2021:0.54✓(n70) 2022:0.38✗(n75) 2023:0.39✗(n83) 2024:0.59✓(n71) 2025:0.58✓(n76)

## Decision: **DEPLOY — MARGINAL (fragile per-window robustness; fund only as live-validation with conservative sizing + a pre-committed kill criterion)**

- Blockers (must ALL hold): FULL gate PASS ∧ OOS-aggregate gate PASS ∧ per-window clause PASS → PASS.
- Adverse-fill stress (FIX 3): OOS-agg gate PASS ∧ window clause PASS → PASS.
- Diagnostic pure_moo FULL Sharpe 1.27 (not live-executable under CLAUDE.md; reported for context only).

### Honest read — this is a FRAGILE pass, not a robust edge
- Under realistic full-spread cost the per-window OOS Sharpes are 0.29–0.64 — the clause clears at EXACTLY the 50% pass-rate floor (3/6), with the weakest passing window at 0.54 (floor 0.5). One window flipping sign would fail it.
- The legacy cert's '6/6 windows, agg 2.13' was ZERO-COST and does NOT transfer: under realistic fill+cost it is 3/6. The aggregate Sharpe (1.23) masks the dispersion — the edge is carried by 2020/2021/2024 and is weak (<0.5) in 2022/2023/2025.
- Full-spread cost barely moved the FULL Sharpe (1.35) — the strategy is NOT cost-fragile; its fragility is signal robustness, not execution cost.

### Operator guidance for the $10k sleeve
- Fund as LIVE-VALIDATION of a marginal edge, NOT high conviction: conservative initial sizing, and a PRE-COMMITTED kill criterion (e.g. retire if forward realized quarterly Sharpe tracks the weak backtest windows rather than the strong ones).
- Re-run this gate (scripts/momentum_fill_recert.py) on each universe / data refresh; the per-window clause is now in the spec gate and must keep clearing.

### FIX 4 — corrected narrative on the pure_moo vs cap difference
The entry AND the ATR stop are BOTH anchored to the SAME signal-day next-open (ts_momentum.py: stop = next_open − atr_mult×ATR; the simulator fills momentum at that same next-open). There is NO 'stop set far below a post-gap entry' — the v1 narrative was wrong. pure_moo differs from the capped live model ONLY by ALSO entering names whose open gapped >3% above prior close. Whether excluding those is a *beneficial selection filter* is an UNTESTED single-configuration hypothesis (it would need testing under an alternative stop / sizing rule before being relied on), not a validated property.

**Decision rule: deploy ONLY if the LIVE full-spread variant clears the FULL + OOS aggregate gate AND the per-window clause. Statistical note: a single OOS Sharpe near 1.0 has SE ~0.6 over ~2.8y — the per-window clause, not the aggregate point estimate, carries the decision.**
