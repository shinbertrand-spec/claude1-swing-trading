# ORB intraday probe — RESEARCH-ONLY, NOT GATE-CLEARED (pilot, 2026-08-04)

**Coarse 1h opening-range breakout over ~2yr of yfinance hourly bars. This CANNOT be walk-forward / DSR / net-cost gated (data too shallow + hourly too coarse). Directional pulse-check only, to decide whether a real intraday data source is worth buying.**

- Trades: 2392 over 2023-09-05..2026-08-03 · tickers ['SPY', 'QQQ', 'NVDA', 'TSLA', 'AMD', 'META']
- Per-trade avg return (net 5bps): -0.038% · hit rate 47.2%
- Daily-strategy annualized Sharpe (indicative): **-2.93** · |MDD| 67.8%

| ticker | trades | avg ret % |
|---|---|---|
| AMD | 348 | -0.056 |
| META | 355 | -0.075 |
| NVDA | 379 | 0.020 |
| QQQ | 467 | -0.025 |
| SPY | 489 | -0.040 |
| TSLA | 354 | -0.059 |

## Indicative read: **NO usable edge in this shallow probe** (NOT a gate verdict — cannot deploy off this).
- Suggest-only; nothing applied.
