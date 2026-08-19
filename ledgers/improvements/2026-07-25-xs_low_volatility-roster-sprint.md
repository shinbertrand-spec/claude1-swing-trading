# Quant-strategist run — xs_low_volatility

- Kind: **xs_low_volatility**
- Source: Blitz & van Vliet 2007 (paperswithbacktest catalog, in-sample Sharpe 0.717 long-short monthly)
- Universe: sp500_leaning_88 (87 tickers + SPY benchmark)
- Period: 2020-01-01 → 2025-12-31
- Walk-forward: rolling
- Deployment gate (aggregate): Sharpe > 1.0 AND |DD| < 25.0% AND n ≥ 30
- Per-window clause: ≥ 50% of OOS windows with Sharpe > 0.5
- Param combinations evaluated: **3**

## Ranked combos

| Rank | Gate | OOS Sharpe | OOS DD | OOS n | OOS CAGR | Params (varied only) |
|---|---|---|---|---|---|---|
| 1 | ❌ | 0.88 | -18.76% | 177 | +11.10% | bottom_n=20 |
| 2 | ❌ | 0.67 | -20.89% | 177 | +8.25% | bottom_n=15 |
| 3 | ❌ | -0.03 | -19.37% | 179 | -1.17% | bottom_n=10 |

## Top combo — full detail

Params:
- `lookback_days`: 60
- `rebalance_period_days`: 21
- `max_hold_days`: 21
- `atr_period`: 20
- `atr_stop_multiple`: 2.5
- `risk_per_trade`: 0.01
- `regime_filter_period`: 200
- `benchmark`: SPY
- `bottom_n`: 20

## OOS — DEPLOYMENT GATE

### Trade stats
- Trades: **177** (wins 84 / losses 93 / breakeven 0)
- Win rate: **47.5%**
- Avg winner: +1.29R · Avg loser: -0.93R
- Expectancy / trade: **+0.12R**
- Profit factor: **1.25**
- Avg bars held: 16.1

### Return stats
- Sharpe (annualised): **0.88**
- Sortino: 3.06 · Calmar: 0.59
- Max drawdown: **-18.76%**
- Cumulative return: +22.44% (CAGR +11.10%)
- Deployment gate (Sharpe > 1.0 AND |DD| < 25% AND n ≥ 30): **❌ FAILED**
- _Note: concurrent-position cap (max_concurrent=8) rejected 263/440 outcomes; stats reflect the 177 that would have been on book_

### Exit reasons
- max_hold: 100
- stop_hit: 69
- gap_through_stop: 8

### Per-grade trade stats
- **B**: n=177 · win_rate=47% · expectancy=+0.12R · PF=1.25

## Top combo — per-window OOS breakdown

| IS window | OOS window | OOS n | OOS Sharpe | OOS DD | Gate |
|---|---|---|---|---|---|
| 2020-01-01..2023-01-01 | 2023-01-01..2024-01-01 | 80 | 0.34 | -18.76% | ❌ |
| 2021-01-01..2024-01-01 | 2024-01-01..2025-01-01 | 97 | 1.28 | -9.04% | ✅ |

## Top combo — tightened gate verdict

- Aggregate clause: Sharpe 0.88 · |MDD| 18.76% · n 177 → **FAILED**
- Per-window clause: 1/2 windows above Sharpe 0.5 (pass rate 0.50, required 0.50) → **PASSED**
- **Composite gate: FAILED**
- _Note: aggregate fails: Sharpe 0.88 <= 1.0_

## Doctrine verdict

Per `swing-risk-compliance-doctrine` + `walk-forward-analysis`: a strategy ships to paper-trade (then live) only when (a) the **aggregated OOS** clears `spec.gate.sharpe_min` / `spec.gate.max_dd_pct` / `spec.gate.n_min`, AND (b) for rolling walk-forward, at least `spec.gate.min_window_pass_rate` of the per-window OOS reports individually clear Sharpe > `spec.gate.min_window_sharpe`. Gate-passing combos are deployable candidates; gate-failing combos are research material.
## 7th gate — Deflated Sharpe (C1)

- **DSR = 0.3415** vs threshold 0.95 -> **FAIL** (report-only)
- E[max SR] under H0 across N=83 trials: 1.18 annualized (SR0)
- inputs: T=440 obs · skew 1.06 · raw kurtosis 4.19 · var(trial SR, per-period) 0.000912
- N = global trial-registry floor (ledgers/trials.yml) — raw count, no correlation shrink: conservative (deflates more)
- **surfaced, not auto-retired** — operator reviews (per C1 spec)
