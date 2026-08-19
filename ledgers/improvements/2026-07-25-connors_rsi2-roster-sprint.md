# Quant-strategist run — connors_cumulative_rsi2

- Kind: **connors_rsi2**
- Source: Connors, *Short Term Trading Strategies That Work* (2008); quantitativo.com 2025 re-validation
- Universe: sp500_leaning_88 (87 tickers + SPY benchmark)
- Period: 2017-01-01 → 2026-05-25
- Walk-forward: rolling
- Deployment gate (aggregate): Sharpe > 1.0 AND |DD| < 25.0% AND n ≥ 30
- Per-window clause: ≥ 50% of OOS windows with Sharpe > 0.5
- Param combinations evaluated: **6**

## Ranked combos

| Rank | Gate | OOS Sharpe | OOS DD | OOS n | OOS CAGR | Params (varied only) |
|---|---|---|---|---|---|---|
| 1 | ❌ | 0.59 | -8.48% | 1392 | +1.46% | entry_threshold=15, cooldown_days=5 |
| 2 | ❌ | 0.55 | -8.28% | 748 | +1.09% | entry_threshold=5, cooldown_days=3 |
| 3 | ❌ | 0.53 | -9.09% | 1456 | +1.34% | entry_threshold=15, cooldown_days=3 |
| 4 | ❌ | 0.45 | -7.83% | 717 | +0.86% | entry_threshold=5, cooldown_days=5 |
| 5 | ❌ | 0.35 | -10.25% | 1176 | +0.82% | entry_threshold=10, cooldown_days=3 |
| 6 | ❌ | 0.31 | -8.68% | 1138 | +0.71% | entry_threshold=10, cooldown_days=5 |

## Top combo — full detail

Params:
- `rsi_period`: 2
- `cumulative_period`: 2
- `regime_sma_period`: 200
- `atr_period`: 20
- `atr_stop_multiple`: 2.0
- `max_hold_days`: 5
- `target_r_multiple`: None
- `risk_per_trade`: 0.002
- `benchmark`: SPY
- `entry_threshold`: 15
- `cooldown_days`: 5

## OOS — DEPLOYMENT GATE

### Trade stats
- Trades: **1392** (wins 709 / losses 681 / breakeven 2)
- Win rate: **50.9%**
- Avg winner: +0.69R · Avg loser: -0.65R
- Expectancy / trade: **+0.03R**
- Profit factor: **1.10**
- Avg bars held: 4.5

### Return stats
- Sharpe (annualised): **0.59**
- Sortino: 1.23 · Calmar: 0.17
- Max drawdown: **-8.48%**
- Cumulative return: +9.11% (CAGR +1.46%)
- Deployment gate (Sharpe > 1.0 AND |DD| < 25% AND n ≥ 30): **❌ FAILED**
- _Note: concurrent-position cap (max_concurrent=8) rejected 1215/2607 outcomes; stats reflect the 1392 that would have been on book_

### Exit reasons
- max_hold: 1101
- stop_hit: 234
- gap_through_stop: 57

### Per-grade trade stats
- **B**: n=1392 · win_rate=51% · expectancy=+0.03R · PF=1.10

## Top combo — per-window OOS breakdown

| IS window | OOS window | OOS n | OOS Sharpe | OOS DD | Gate |
|---|---|---|---|---|---|
| 2017-01-01..2020-01-01 | 2020-01-01..2021-01-01 | 200 | 0.61 | -4.18% | ❌ |
| 2018-01-01..2021-01-01 | 2021-01-01..2022-01-01 | 296 | -0.15 | -3.68% | ❌ |
| 2019-01-01..2022-01-01 | 2022-01-01..2023-01-01 | 48 | -12.99 | -4.71% | ❌ |
| 2020-01-01..2023-01-01 | 2023-01-01..2024-01-01 | 254 | 0.17 | -3.14% | ❌ |
| 2021-01-01..2024-01-01 | 2024-01-01..2025-01-01 | 314 | 1.91 | -3.06% | ✅ |
| 2022-01-01..2025-01-01 | 2025-01-01..2026-01-01 | 283 | 2.35 | -1.85% | ✅ |

## Top combo — tightened gate verdict

- Aggregate clause: Sharpe 0.59 · |MDD| 8.48% · n 1392 → **FAILED**
- Per-window clause: 3/6 windows above Sharpe 0.5 (pass rate 0.50, required 0.50) → **PASSED**
- **Composite gate: FAILED**
- _Note: aggregate fails: Sharpe 0.59 <= 1.0_

## Doctrine verdict

Per `swing-risk-compliance-doctrine` + `walk-forward-analysis`: a strategy ships to paper-trade (then live) only when (a) the **aggregated OOS** clears `spec.gate.sharpe_min` / `spec.gate.max_dd_pct` / `spec.gate.n_min`, AND (b) for rolling walk-forward, at least `spec.gate.min_window_pass_rate` of the per-window OOS reports individually clear Sharpe > `spec.gate.min_window_sharpe`. Gate-passing combos are deployable candidates; gate-failing combos are research material.
## 7th gate — Deflated Sharpe (C1)

- **DSR = 0.8456** vs threshold 0.95 -> **FAIL** (report-only)
- E[max SR] under H0 across N=83 trials: 0.28 annualized (SR0)
- inputs: T=2607 obs · skew 0.60 · raw kurtosis 4.23 · var(trial SR, per-period) 0.000050
- N = global trial-registry floor (ledgers/trials.yml) — raw count, no correlation shrink: conservative (deflates more)
- **surfaced, not auto-retired** — operator reviews (per C1 spec)
