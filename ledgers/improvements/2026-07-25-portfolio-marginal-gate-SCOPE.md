# Portfolio-marginal gate — scope + pre-registration (2026-07-25)

## Problem it solves

The current deployment gate judges a candidate **standalone**: OOS Sharpe > 1.0
(∧ |MDD| < 25% ∧ n ≥ 30 ∧ per-window ≥ 50%), + net-of-cost + DSR. That is the
correct test for a **first** strategy. It is the **wrong** test for a **second**
(diversifier) sleeve: a candidate with standalone Sharpe 0.5 that is decorrelated
from the book can *lower the book's drawdown* (raise its Calmar) even though it
never clears 1.0 alone. The cross-asset trend work proved this concretely
(standalone 0.38–0.52, but 50/50 with ts_momentum cut drawdown ~17%→~10% at flat
Sharpe). Under the standalone gate, no such diversifier can EVER be promoted.

The portfolio-marginal gate asks the right question for a second sleeve:
**does adding this candidate improve the BOOK, out-of-sample?**

## The discipline problem (why this needs teeth)

A marginal gate is dangerous — it can rationalize adding junk ("it's uncorrelated,
it helps the sample"). So it is bounded by four clauses, and the thresholds are
**pre-registered below BEFORE running** the candidate, so the result cannot be
gerrymandered.

## Gate definition — PORTFOLIO-MARGINAL GATE v1 (PRE-REGISTERED)

Book B = current roster (today: ts_momentum_liquid_us alone).
Candidate C = diversifier sleeve.
Combine at **walk-forward risk-parity**: each sleeve vol-targeted to 10% annual
using its **trailing-year** realized vol (no look-ahead), then 50/50. Evaluated
on **aggregated rolling 1-year OOS** windows.

Metrics (all OOS, net-of-nothing-extra — psim already net-of-cost):
- **Sharpe** = mean/std × √252
- **Calmar** = annualized return / |max drawdown|  (return-per-unit-drawdown)

A candidate PASSES iff ALL of:

| clause | condition | rationale |
|---|---|---|
| **M1 Sharpe non-destruction** | combined Sharpe ≥ **0.90 × book Sharpe** | the sleeve may cost ≤10% of risk-adjusted return, no more |
| **M2 material book improvement** | combined Calmar ≥ **1.10 × book Calmar** | must improve return-per-drawdown by ≥10% (the diversifier's job) |
| **M3 robustness** | combined Calmar > book Calmar in **≥ 50%** of OOS windows | not a one-window fluke |
| **M4 genuine diversifier** | aggregate OOS corr(C, B) < **0.70** | not a closet clone that just boosts the sample |

PASS = M1 ∧ M2 ∧ M3 ∧ M4. A PASS promotes C as a **diversifier sleeve at the
risk-parity weight** — NOT as a standalone strategy, and NOT to full weight.

## What it deliberately does NOT do

- Does not lower the STANDALONE bar for a first/primary strategy (that stays
  Sharpe>1.0 + net-cost + DSR). This gate is a SECOND, additive path for
  diversifier sleeves only.
- Does not use in-sample-optimized weights (walk-forward risk-parity only).
- Multiple-testing caveat: running this across many candidate C's inflates false
  positives, exactly like the standalone DSR gate. A **marginal-DSR analogue**
  (deflate the Calmar improvement by the number of candidate sleeves tried) is
  future work. For now N=1 candidate (cross-asset), so the risk is minimal — but
  every future candidate MUST be logged (like ledgers/trials.yml) so the floor
  is honest.

## Backtest plan

Apply the gate to candidate C = **long-only crypto-free cross-asset trend**
(TLT/IEF/GLD/SLV/DBC/DBA/USO/UUP, lookback 126) against book B =
**ts_momentum_liquid_us** (lookback 252, top_k 8), both via psim (consistent
net-of-cost framework), rolling 1-year OOS windows 2019–2025 (trailing year seeds
the risk-parity weight; covers 2020 COVID + 2022 rate-shock).

VALIDATION: book-alone aggregate OOS Sharpe must ≈ ts_momentum's known ~1.0–1.3,
or the harness is not trusted (the v1 measurement-bug lesson).

Report: per-window + aggregate book-vs-combined Sharpe/MDD/Calmar, correlation,
per-clause PASS/FAIL, overall verdict. Promotion remains the operator's call.
