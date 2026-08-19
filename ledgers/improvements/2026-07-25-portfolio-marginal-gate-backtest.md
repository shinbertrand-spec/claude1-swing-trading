# Portfolio-marginal gate v1 — backtest on cross-asset diversifier

- Book B = ts_momentum_liquid_us · Candidate C = cross-asset long-only (crypto-free)
- Rolling 1-yr OOS 2019-2025, walk-forward risk-parity (trailing-yr vol-target 10%, 50/50)
- **VALIDATION:** book-alone agg OOS Sharpe = **1.51** (ts ref ~1.0-1.3 — PASS)

## Per-window (vol-matched)

| OOS yr | book Sharpe | comb Sharpe | book Calmar | comb Calmar | comb>book Calmar? |
|---|---|---|---|---|---|
| 2019 | 1.87 | 2.27 | 3.70 | 2.64 | no |
| 2020 | 2.00 | 2.25 | 2.99 | 3.80 | yes |
| 2021 | 0.93 | 1.45 | 1.21 | 1.79 | yes |
| 2022 | 0.46 | 0.64 | 0.48 | 0.62 | yes |
| 2023 | 1.76 | 0.24 | 3.99 | 0.26 | no |
| 2024 | 1.66 | 1.23 | 2.55 | 1.87 | no |
| 2025 | 1.61 | 1.98 | 2.43 | 2.55 | yes |

## Aggregate + gate verdict

| metric | book (ts alone) | combined (ts + cross-asset) |
|---|---|---|
| Sharpe | 1.51 | 1.50 |
| Calmar | 1.79 | 1.61 |
| MDD % | -10.2 | -8.2 |
| corr(C,B) | — | +0.116 |

| clause | condition | value | verdict |
|---|---|---|---|
| M1 Sharpe non-destruction | comb ≥ 0.90×book (1.36) | 1.50 | PASS |
| M2 Calmar improvement | comb ≥ 1.10×book (1.97) | 1.61 | FAIL |
| M3 robustness | comb>book Calmar in ≥50% windows | 4/7 | PASS |
| M4 genuine diversifier | corr < 0.70 | +0.116 | PASS |

### OVERALL: **FAIL — not promotable under the pre-registered gate**

_Weights are walk-forward risk-parity (no look-ahead). Thresholds pre-registered in 2026-07-25-portfolio-marginal-gate-SCOPE.md before this run._
