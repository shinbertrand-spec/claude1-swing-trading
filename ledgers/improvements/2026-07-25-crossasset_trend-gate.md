# Quant-strategist run — crossasset_trend

- Kind: **ts_momentum**
- Source: Moskowitz-Ooi-Pedersen 2012 (TS momentum) applied cross-asset; Clenow managed-futures diversification
- Universe: inline (10 tickers + SPY benchmark)
- Period: 2010-01-01 → 2026-05-25
- Walk-forward: rolling
- Deployment gate (aggregate): Sharpe > 1.0 AND |DD| < 25.0% AND n ≥ 30
- Per-window clause: ≥ 50% of OOS windows with Sharpe > 0.5
- Param combinations evaluated: **2**

## Ranked combos

| Rank | Gate | OOS Sharpe | OOS DD | OOS n | OOS CAGR | Params (varied only) |
|---|---|---|---|---|---|---|
| 1 | ❌ | 0.80 | -17.84% | 629 | +7.13% | lookback_days=126 |
| 2 | ❌ | 0.45 | -21.88% | 580 | +3.53% | lookback_days=252 |

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
- Trades: **629** (wins 308 / losses 321 / breakeven 0)
- Win rate: **49.0%**
- Avg winner: +1.21R · Avg loser: -0.87R
- Expectancy / trade: **+0.15R**
- Profit factor: **1.34**
- Avg bars held: 16.6

### Return stats
- Sharpe (annualised): **0.80**
- Sortino: 2.85 · Calmar: 0.40
- Max drawdown: **-17.84%**
- Cumulative return: +145.38% (CAGR +7.13%)
- Deployment gate (Sharpe > 1.0 AND |DD| < 25% AND n ≥ 30): **❌ FAILED**

### Exit reasons
- max_hold: 392
- stop_hit: 163
- gap_through_stop: 74

### Per-grade trade stats
- **B**: n=629 · win_rate=49% · expectancy=+0.15R · PF=1.34

## Top combo — per-window OOS breakdown

| IS window | OOS window | OOS n | OOS Sharpe | OOS DD | Gate |
|---|---|---|---|---|---|
| 2010-01-01..2013-01-01 | 2013-01-01..2014-01-01 | 23 | -0.21 | -6.76% | ❌ |
| 2011-01-01..2014-01-01 | 2014-01-01..2015-01-01 | 49 | 1.58 | -9.76% | ✅ |
| 2012-01-01..2015-01-01 | 2015-01-01..2016-01-01 | 25 | -0.35 | -5.93% | ❌ |
| 2013-01-01..2016-01-01 | 2016-01-01..2017-01-01 | 60 | 0.70 | -10.39% | ❌ |
| 2014-01-01..2017-01-01 | 2017-01-01..2018-01-01 | 42 | -0.43 | -12.03% | ❌ |
| 2015-01-01..2018-01-01 | 2018-01-01..2019-01-01 | 40 | -0.46 | -5.69% | ❌ |
| 2016-01-01..2019-01-01 | 2019-01-01..2020-01-01 | 64 | 1.65 | -8.89% | ✅ |
| 2017-01-01..2020-01-01 | 2020-01-01..2021-01-01 | 55 | 1.41 | -9.46% | ✅ |
| 2018-01-01..2021-01-01 | 2021-01-01..2022-01-01 | 57 | 1.93 | -7.49% | ✅ |
| 2019-01-01..2022-01-01 | 2022-01-01..2023-01-01 | 44 | 2.03 | -3.96% | ✅ |
| 2020-01-01..2023-01-01 | 2023-01-01..2024-01-01 | 50 | -1.16 | -11.32% | ❌ |
| 2021-01-01..2024-01-01 | 2024-01-01..2025-01-01 | 63 | 0.49 | -10.01% | ❌ |
| 2022-01-01..2025-01-01 | 2025-01-01..2026-01-01 | 57 | 1.22 | -6.88% | ✅ |

## Top combo — tightened gate verdict

- Aggregate clause: Sharpe 0.80 · |MDD| 17.84% · n 629 → **FAILED**
- Per-window clause: 7/13 windows above Sharpe 0.5 (pass rate 0.54, required 0.50) → **PASSED**
- **Composite gate: FAILED**
- _Note: aggregate fails: Sharpe 0.80 <= 1.0_

## Doctrine verdict

Per `swing-risk-compliance-doctrine` + `walk-forward-analysis`: a strategy ships to paper-trade (then live) only when (a) the **aggregated OOS** clears `spec.gate.sharpe_min` / `spec.gate.max_dd_pct` / `spec.gate.n_min`, AND (b) for rolling walk-forward, at least `spec.gate.min_window_pass_rate` of the per-window OOS reports individually clear Sharpe > `spec.gate.min_window_sharpe`. Gate-passing combos are deployable candidates; gate-failing combos are research material.
## 7th gate — Deflated Sharpe (C1)

- **DSR = 0.6237** vs threshold 0.95 -> **FAIL** (report-only)
- E[max SR] under H0 across N=83 trials: 0.60 annualized (SR0)
- inputs: T=629 obs · skew 1.34 · raw kurtosis 6.36 · var(trial SR, per-period) 0.000238
- N = global trial-registry floor (ledgers/trials.yml) — raw count, no correlation shrink: conservative (deflates more)
- **surfaced, not auto-retired** — operator reviews (per C1 spec)
