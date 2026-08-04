# Futures TSMOM long/short — YAHOO CONTINUOUS (free real-futures pass) — net-of-cost gate (pilot, 2026-08-04)

34 Yahoo =F continuous contracts across ['energy', 'equity', 'fx', 'grains', 'metals', 'rates', 'softs']. 2007-01-01..2026-05-22. 12-1 (+blend) TSMOM, inverse-vol weight, 35% sector cap, 12% vol target, 2bps cost. **Roll-artifact winsorize: 79/161953 daily returns clipped at ±15% (0.05%).**

| variant | Sharpe | |MDD|% | cum% | OOS yrs>0.5 | DSR | GATE |
|---|---|---|---|---|---|---|
| L/S 12-1 | 0.30 | 35.9 | 71 | 3/6 | 0.48 | FAIL |
| L/S 3-6-12 blend | -0.03 | 48.9 | -17 | 2/6 | 0.07 | FAIL |
| LONG-only 12-1 | 0.43 | 34.2 | 146 | 3/6 | 0.71 | FAIL |

- L/S ↔ ES(S&P) correlation: **+0.06**

### Per-OOS-year Sharpe
| year | L/S 12-1 | L/S 3-6-12 blend | LONG-only 12-1 |
|---|---|---|---|
| 2020 | -0.34 | -0.82 | 0.13 |
| 2021 | 0.76 | 1.37 | 0.70 |
| 2022 | 1.29 | 0.75 | -0.04 |
| 2023 | -0.64 | -1.25 | 0.56 |
| 2024 | 0.06 | -1.12 | 0.28 |
| 2025 | 1.27 | -0.21 | 1.45 |

## Verdict: **no variant clears the gate on free real futures**
- Free real-futures upgrade over the ETF proxy; roll-gap noise is the residual (winsorized above).
- Confirmatory step if a pulse appears: the clean Databento back-adjusted pull (spec on file).
- Suggest-only; nothing applied.
