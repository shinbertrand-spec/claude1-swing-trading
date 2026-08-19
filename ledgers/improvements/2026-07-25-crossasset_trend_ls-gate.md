# Quant-strategist run — crossasset_trend_ls

- Kind: **ts_momentum**
- Source: TS momentum applied cross-asset long/short via -1x inverse ETFs (managed-futures analogue, long-only implementation)
- Universe: inline (15 tickers + SPY benchmark)
- Period: 2010-01-01 → 2026-05-25
- Walk-forward: rolling
- Deployment gate (aggregate): Sharpe > 1.0 AND |DD| < 25.0% AND n ≥ 30
- Per-window clause: ≥ 50% of OOS windows with Sharpe > 0.5
- Param combinations evaluated: **2**

## Ranked combos

| Rank | Gate | OOS Sharpe | OOS DD | OOS n | OOS CAGR | Params (varied only) |
|---|---|---|---|---|---|---|
| 1 | ❌ | 0.44 | -29.18% | 912 | +4.17% | lookback_days=126 |
| 2 | ❌ | 0.08 | -44.11% | 845 | +0.28% | lookback_days=252 |

## Top combo — full detail

Params:
- `rebalance_period_days`: 21
- `max_hold_days`: 21
- `top_k`: 8
- `atr_period`: 20
- `atr_stop_multiple`: 3.0
- `risk_per_trade`: 0.01
- `benchmark`: SPY
- `lookback_days`: 126

## OOS — DEPLOYMENT GATE

### Trade stats
- Trades: **912** (wins 411 / losses 499 / breakeven 2)
- Win rate: **45.1%**
- Avg winner: +1.18R · Avg loser: -0.85R
- Expectancy / trade: **+0.07R**
- Profit factor: **1.14**
- Avg bars held: 16.6

### Return stats
- Sharpe (annualised): **0.44**
- Sortino: 1.52 · Calmar: 0.14
- Max drawdown: **-29.18%**
- Cumulative return: +70.31% (CAGR +4.17%)
- Deployment gate (Sharpe > 1.0 AND |DD| < 25% AND n ≥ 30): **❌ FAILED**

### Exit reasons
- max_hold: 560
- stop_hit: 238
- gap_through_stop: 114

### Per-grade trade stats
- **B**: n=912 · win_rate=45% · expectancy=+0.07R · PF=1.14

## Top combo — per-window OOS breakdown

| IS window | OOS window | OOS n | OOS Sharpe | OOS DD | Gate |
|---|---|---|---|---|---|
| 2010-01-01..2013-01-01 | 2013-01-01..2014-01-01 | 47 | 0.34 | -7.34% | ❌ |
| 2011-01-01..2014-01-01 | 2014-01-01..2015-01-01 | 63 | 1.61 | -9.76% | ✅ |
| 2012-01-01..2015-01-01 | 2015-01-01..2016-01-01 | 61 | 0.04 | -10.57% | ❌ |
| 2013-01-01..2016-01-01 | 2016-01-01..2017-01-01 | 78 | -0.21 | -13.65% | ❌ |
| 2014-01-01..2017-01-01 | 2017-01-01..2018-01-01 | 53 | -0.90 | -14.79% | ❌ |
| 2015-01-01..2018-01-01 | 2018-01-01..2019-01-01 | 72 | 0.17 | -6.38% | ❌ |
| 2016-01-01..2019-01-01 | 2019-01-01..2020-01-01 | 82 | -0.09 | -12.27% | ❌ |
| 2017-01-01..2020-01-01 | 2020-01-01..2021-01-01 | 68 | 0.87 | -11.74% | ❌ |
| 2018-01-01..2021-01-01 | 2021-01-01..2022-01-01 | 76 | 1.63 | -9.22% | ✅ |
| 2019-01-01..2022-01-01 | 2022-01-01..2023-01-01 | 94 | 1.91 | -12.70% | ✅ |
| 2020-01-01..2023-01-01 | 2023-01-01..2024-01-01 | 77 | -1.34 | -13.35% | ❌ |
| 2021-01-01..2024-01-01 | 2024-01-01..2025-01-01 | 69 | 0.11 | -11.44% | ❌ |
| 2022-01-01..2025-01-01 | 2025-01-01..2026-01-01 | 72 | 0.55 | -8.66% | ❌ |

## Top combo — tightened gate verdict

- Aggregate clause: Sharpe 0.44 · |MDD| 29.18% · n 912 → **FAILED**
- Per-window clause: 5/13 windows above Sharpe 0.5 (pass rate 0.38, required 0.50) → **FAILED**
- **Composite gate: FAILED**
- _Note: aggregate fails: Sharpe 0.44 <= 1.0; |MDD| 29.18% >= 25.0% | only 5/13 windows have Sharpe > 0.5 (pass rate 0.38 < 0.5)_

## Doctrine verdict

Per `swing-risk-compliance-doctrine` + `walk-forward-analysis`: a strategy ships to paper-trade (then live) only when (a) the **aggregated OOS** clears `spec.gate.sharpe_min` / `spec.gate.max_dd_pct` / `spec.gate.n_min`, AND (b) for rolling walk-forward, at least `spec.gate.min_window_pass_rate` of the per-window OOS reports individually clear Sharpe > `spec.gate.min_window_sharpe`. Gate-passing combos are deployable candidates; gate-failing combos are research material.
## 7th gate — Deflated Sharpe (C1)

- **DSR = 0.3540** vs threshold 0.95 -> **FAIL** (report-only)
- E[max SR] under H0 across N=83 trials: 0.64 annualized (SR0)
- inputs: T=912 obs · skew 1.33 · raw kurtosis 6.15 · var(trial SR, per-period) 0.000264
- N = global trial-registry floor (ledgers/trials.yml) — raw count, no correlation shrink: conservative (deflates more)
- **surfaced, not auto-retired** — operator reviews (per C1 spec)
