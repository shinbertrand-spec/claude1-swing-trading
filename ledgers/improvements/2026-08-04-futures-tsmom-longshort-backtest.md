# Futures TSMOM long/short (proxy ETFs) — net-of-cost gate (pilot, 2026-08-04)

Universe ['SPY', 'QQQ', 'IWM', 'EFA', 'EEM', 'TLT', 'IEF', 'GLD', 'SLV', 'USO', 'DBC', 'DBA', 'UUP', 'FXE', 'FXY', 'VNQ'] across ['equity', 'rates', 'commod', 'fx', 'reit']. 2010-01-01..2026-05-25. 12-1 TSMOM sign, inverse-vol risk weight, monthly rebalance, 3bps turnover cost. Shorts = short the cash ETF (no inverse-ETF decay). ETF≠futures caveat (roll/financing).

| strategy | Sharpe | |MDD|% | cum% | OOS yrs>0.5 | DSR | GATE |
|---|---|---|---|---|---|---|
| L/S TSMOM | 0.15 | 16.4 | 12 | 2/6 | 0.36 | FAIL |
| LONG-only TSMOM | 0.40 | 14.4 | 59 | 3/6 | 0.75 | FAIL |
| SPY buy&hold | 0.76 | 34.1 | 558 | 5/6 | 0.98 | FAIL |

- L/S ↔ SPY correlation: **+0.25** (diversification vs our equity-long book)

### Per-OOS-year Sharpe
| year | L/S | LONG | SPY |
|---|---|---|---|
| 2020 | -0.65 | 0.21 | 0.61 |
| 2021 | 0.35 | 0.71 | 1.89 |
| 2022 | 0.64 | -0.54 | -0.78 |
| 2023 | -1.29 | -0.12 | 1.73 |
| 2024 | 0.29 | 0.75 | 1.73 |
| 2025 | 1.25 | 1.71 | 0.88 |

## Verdict: **no TSMOM variant clears the gate**
- Key question was diversification (low/neg corr to our equity-long edge), not just standalone Sharpe.
- Real futures (roll-adjusted continuous contracts) needed to confirm — this is the free ETF-proxy first pass.
- Suggest-only; nothing applied.
