# Portfolio-marginal gate v2 (Sharpe-proportional weight) — cross-asset diversifier

- Book B = ts_momentum_liquid_us · Candidate C = cross-asset long-only (crypto-free)
- Rolling 1-yr OOS 2019-2025 · weight = walk-forward Sharpe-proportional (trailing-yr)
- **VALIDATION:** book-alone agg OOS Sharpe = **1.51** (PASS)

## Per-window

| OOS yr | cross-asset wt | book Sharpe | comb Sharpe | book Calmar | comb Calmar | comb>book? |
|---|---|---|---|---|---|---|
| 2019 | 100% | 1.87 | 1.13 | 3.70 | 1.29 | no |
| 2020 | 38% | 2.00 | 2.33 | 2.99 | 3.94 | yes |
| 2021 | 41% | 0.93 | 1.38 | 1.21 | 1.71 | yes |
| 2022 | 59% | 0.46 | 0.65 | 0.48 | 0.63 | yes |
| 2023 | 57% | 1.76 | -0.11 | 3.99 | -0.13 | no |
| 2024 | 0% | 1.66 | 1.66 | 2.55 | 2.55 | no |
| 2025 | 6% | 1.61 | 1.68 | 2.43 | 2.47 | yes |

## Aggregate + gate verdict

| metric | book (ts alone) | combined (Sharpe-prop) |
|---|---|---|
| Sharpe | 1.51 | 1.35 |
| Calmar | 1.79 | 1.37 |
| MDD % | -10.2 | -10.2 |
| corr(C,B) | — | +0.116 |

| clause | condition | value | verdict |
|---|---|---|---|
| M1 Sharpe non-destruction | comb ≥ 0.90×book (1.36) | 1.35 | FAIL |
| M2 Calmar improvement | comb ≥ 1.10×book (1.97) | 1.37 | FAIL |
| M3 robustness | comb>book Calmar in ≥50% windows | 4/7 | PASS |
| M4 genuine diversifier | corr < 0.70 | +0.116 | PASS |

### OVERALL: **FAIL — not promotable (v2 is the final authorized weight rule; no further iteration)**

_Weight rule pre-registered in 2026-07-25-portfolio-marginal-gate-v2-SCOPE.md before this run. Gate clauses/thresholds identical to v1._
