# Strategy #4 — Turn-of-month seasonality — net-of-cost gate (pilot, 2026-08-04)

Universe ['SPY', 'QQQ', 'IWM']+basket. 2017-01-01..2026-05-25. Long last 1 + first 3 trading days/month, flat else. Cost charged per enter/exit flip. Gate: Sharpe>1.0 & |MDD|<25% & >=50% of 6 OOS yrs clear Sharpe>0.5 & DSR>0.95, net.

| instrument | cost bps | net Sharpe | |MDD|% | cum% | OOS yrs>0.5 | DSR | GATE |
|---|---|---|---|---|---|---|---|
| SPY | 0 | 0.44 | 9.9 | 33 | 2/6 | 0.69 | FAIL |
| SPY | 5 | 0.28 | 10.6 | 19 | 2/6 | 0.51 | FAIL |
| SPY | 10 | 0.12 | 13.7 | 6 | 2/6 | 0.33 | FAIL |
| QQQ | 0 | 0.53 | 14.4 | 56 | 3/6 | 0.78 | FAIL |
| QQQ | 5 | 0.41 | 15.5 | 39 | 2/6 | 0.66 | FAIL |
| QQQ | 10 | 0.29 | 16.8 | 24 | 2/6 | 0.52 | FAIL |
| IWM | 0 | 0.10 | 24.9 | 5 | 2/6 | 0.30 | FAIL |
| IWM | 5 | -0.02 | 26.2 | -7 | 2/6 | 0.18 | FAIL |
| IWM | 10 | -0.14 | 31.1 | -17 | 1/6 | 0.10 | FAIL |
| BASKET | 0 | 0.37 | 13.1 | 30 | 2/6 | 0.62 | FAIL |
| BASKET | 5 | 0.23 | 15.2 | 16 | 2/6 | 0.45 | FAIL |
| BASKET | 10 | 0.09 | 19.3 | 4 | 2/6 | 0.29 | FAIL |

## Verdict: **NO net variant clears the gate**
- Low-turnover so cost is not the killer here; the question is whether the seasonal Sharpe is high enough + robust across years.
- Suggest-only; nothing applied.
