## Live-vs-backtest tracking report (B3)

- asof: 2026-07-24 · base equity $1,000,000 (Sharpe/Sortino/PSR are base-invariant; %-figures are not)
- trades reconstructed: 10

### Sleeve (all setups, daily mark-to-market)

- T = 27 trading days — PSR at this sample size is weak evidence by construction; it firms up as T grows
- cum return -0.42% · Sharpe(ann) -1.27 · Sortino(ann) -1.68 · maxDD -0.84% · vol(ann) 3.08%
- **PSR vs backtest benchmark 1.39 (ann, notional-weighted across setup benchmarks)**: **19.1%** = P(true live Sharpe exceeds benchmark)

### Per setup

| setup | T | active | cum % | Sharpe | benchmark (ann) | PSR | note |
|---|---|---|---|---|---|---|---|
| xs_short_term_reversal | 27 | 9 | -0.40 | -4.66 | 1.60 (rolling_agg_sharpe) | 0.4% |  |
| residual_momentum_liquid_us | 27 | 25 | 0.74 | 5.71 | 1.09 (rolling_agg_sharpe) | 91.4% |  |
| xs_short_term_reversal_liquid_us | 27 | 18 | -0.31 | -4.45 | 1.35 (rolling_agg_sharpe) | 2.0% |  |
| connors_rsi2 | 27 | 13 | 0.14 | 1.56 | 1.01 (rolling_agg_sharpe) | 56.9% |  |
| clenow_momentum_liquid_us | 27 | 9 | -0.58 | -2.57 | 1.47 (rolling_agg_sharpe) | 8.2% |  |

### Expectation cone (backtest Sharpe x live realized vol)

| horizon (days) | -2σ % | -1σ % | expected % | +1σ % | +2σ % | actual % | z |
|---|---|---|---|---|---|---|---|
| 5 | -0.78 | -0.35 | 0.08 | 0.52 | 0.95 | — | — |
| 10 | -1.06 | -0.44 | 0.17 | 0.78 | 1.40 | — | — |
| 21 | -1.42 | -0.53 | 0.36 | 1.25 | 2.14 | -0.42 | -0.87 |
| 42 | -1.80 | -0.54 | 0.71 | 1.97 | 3.23 | — | — |
| 63 | -2.01 | -0.47 | 1.07 | 2.61 | 4.15 | — | — |
| 126 | -2.22 | -0.04 | 2.14 | 4.32 | 6.50 | — | — |
| 252 | -1.88 | 1.20 | 4.28 | 7.36 | 10.44 | — | — |

### Reconstruction notes

- AMD: entry expired unfilled — no trade to reconstruct
- CAT: entry expired unfilled — no trade to reconstruct
- GOOGL: entry expired unfilled — no trade to reconstruct
- NFLX: closed without exit_price — skipped (mirrors performance.py)
