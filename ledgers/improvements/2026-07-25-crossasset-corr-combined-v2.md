# Cross-asset trend v2 (crypto-free) — decorrelation + combined book

- Common window: **2021-06-23 → 2026-05-25** (1235 weekday days; covers 2022 rate-shock, 2023-24 bull, 2025)
- Cross-asset book: TLT, IEF, GLD, SLV, DBC, DBA, USO, UUP (crypto removed — clean weekday calendar)
- **OOS correlation ts vs cross-asset: r = +0.205**

| book | ann. Sharpe | MDD% | days |
|---|---|---|---|
| ts_momentum ALONE (baseline) | 1.06 | -17.3 | 1235 |
| cross-asset ALONE (crypto-free) | 0.38 | -7.9 | 1235 |
| 50/50 equal blend | 1.07 | -9.6 | 1235 |
| inverse-vol (0.18 ts / 0.82 ca) | 0.93 | -6.8 | 1235 |

Standalone Sharpes here are computed on the fresh-window sim (weekday-joined) — compare to psim's reported figures printed to the log as the validation check.

**Read:** if the blends beat ts_momentum ALONE, the cross-asset sleeve is additive at the PORTFOLIO level despite failing the STANDALONE gate — the case for a portfolio-marginal gate. 50/50 and inverse-vol carry no look-ahead.
