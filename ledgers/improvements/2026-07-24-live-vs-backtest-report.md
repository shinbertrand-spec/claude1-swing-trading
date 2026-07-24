## Live-vs-backtest tracking report (B3)

- asof: 2026-07-24 · base equity $1,000,000 (Sharpe/Sortino/PSR are base-invariant; %-figures are not)
- trades reconstructed: 7

### Sleeve (all setups, daily mark-to-market)

- T = 27 trading days — PSR at this sample size is weak evidence by construction; it firms up as T grows
- cum return -0.08% · Sharpe(ann) -0.22 · Sortino(ann) -0.33 · maxDD -0.80% · vol(ann) 3.29%
- **PSR vs backtest benchmark 1.37 (ann, notional-weighted across setup benchmarks)**: **30.5%** = P(true live Sharpe exceeds benchmark)

### Per setup

| setup | T | active | cum % | Sharpe | benchmark (ann) | PSR | note |
|---|---|---|---|---|---|---|---|
| residual_momentum_liquid_us | 27 | 25 | 0.74 | 5.71 | 1.09 (rolling_agg_sharpe) | 91.4% |  |
| clenow_momentum_liquid_us | 27 | 9 | -0.58 | -2.57 | 1.47 (rolling_agg_sharpe) | 8.2% |  |
| connors_rsi2 | 27 | 22 | -0.04 | -0.30 | 1.01 (rolling_agg_sharpe) | 33.6% |  |
| xs_short_term_reversal | 27 | 9 | -0.19 | -1.49 | 1.60 (rolling_agg_sharpe) | 16.4% |  |

### Expectation cone (backtest Sharpe x live realized vol)

| horizon (days) | -2σ % | -1σ % | expected % | +1σ % | +2σ % | actual % | z |
|---|---|---|---|---|---|---|---|
| 5 | -0.84 | -0.37 | 0.09 | 0.55 | 1.02 | — | — |
| 10 | -1.13 | -0.48 | 0.18 | 0.83 | 1.49 | — | — |
| 21 | -1.52 | -0.57 | 0.38 | 1.33 | 2.28 | -0.08 | -0.52 |
| 42 | -1.94 | -0.59 | 0.75 | 2.09 | 3.44 | — | — |
| 63 | -2.17 | -0.52 | 1.13 | 2.77 | 4.42 | — | — |
| 126 | -2.40 | -0.08 | 2.25 | 4.58 | 6.90 | — | — |
| 252 | -2.08 | 1.21 | 4.50 | 7.79 | 11.08 | — | — |

### Reconstruction notes

- AMD: closed without exit_price — skipped (mirrors performance.py)
- CAT: closed without exit_price — skipped (mirrors performance.py)
- COIN: closed without exit_price — skipped (mirrors performance.py)
- GOOGL: closed without exit_price — skipped (mirrors performance.py)
- INTU: closed without exit_price — skipped (mirrors performance.py)
- MO: closed without exit_price — skipped (mirrors performance.py)
- VAL: closed without exit_price — skipped (mirrors performance.py)
