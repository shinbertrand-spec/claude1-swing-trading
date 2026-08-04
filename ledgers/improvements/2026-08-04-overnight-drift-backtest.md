# Strategy #1 — Overnight drift (close->open hold) — net-of-cost gate (pilot, 2026-08-04)

Universe: SPY, QQQ, IWM + equal-weight basket. Period 2017-01-01..2026-05-25. Signal: hold long overnight (buy prior close, sell today open), flat intraday. Round-trip cost charged PER DAY. Gate: Sharpe>1.0 & |MDD|<25% & >=50% of 6 OOS years clear Sharpe>0.5 & Deflated Sharpe>0.95, ALL net of cost.

| instrument | cost bps | net Sharpe | |MDD|% | cum% | OOS yrs>0.5 | DSR | GATE |
|---|---|---|---|---|---|---|---|
| SPY | 0 | 0.77 | 29.4 | 118 | 5/6 | 0.96 | FAIL |
| SPY | 3 | 0.13 | 30.1 | 8 | 2/6 | 0.47 | FAIL |
| SPY | 5 | -0.30 | 39.1 | -33 | 1/6 | 0.08 | FAIL |
| QQQ | 0 | 0.93 | 27.4 | 204 | 5/6 | 0.99 | FAIL |
| QQQ | 3 | 0.38 | 33.7 | 50 | 4/6 | 0.75 | FAIL |
| QQQ | 5 | 0.02 | 38.9 | -6 | 2/6 | 0.34 | FAIL |
| IWM | 0 | 1.01 | 29.1 | 264 | 4/6 | 0.99 | FAIL |
| IWM | 3 | 0.50 | 30.0 | 79 | 3/6 | 0.85 | FAIL |
| IWM | 5 | 0.16 | 32.9 | 12 | 3/6 | 0.50 | FAIL |
| BASKET(SPY,QQQ,IWM) | 0 | 0.95 | 28.4 | 191 | 5/6 | 0.99 | FAIL |
| BASKET(SPY,QQQ,IWM) | 3 | 0.36 | 29.3 | 44 | 3/6 | 0.74 | FAIL |
| BASKET(SPY,QQQ,IWM) | 5 | -0.03 | 35.2 | -10 | 3/6 | 0.29 | FAIL |

## Per-year OOS Sharpe — basket @ 3bps (realistic)
| 2020 | 2021 | 2022 | 2023 | 2024 | 2025 |
|---|---|---|---|---|---|
| 0.71 | 1.39 | -1.54 | 0.07 | 1.63 | 0.10 |

## Verdict
- Gross (0bps) overnight Sharpe is real (SPY 0.77) but the edge is ~a few bps/day, so daily round-trip cost is the whole game.
- SPY: gross 0.77 -> net@3bps 0.13.
- **NO net-of-cost variant clears the gate.**
- Suggest-only. Nothing applied.
