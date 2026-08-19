# Quant-strategist run — xs_short_term_reversal

- Kind: **xs_short_term_reversal**
- Source: Lehmann 1990 / Jegadeesh 1990 (paperswithbacktest catalog, in-sample Sharpe 0.816 long-short weekly)
- Universe: sp500_leaning_88 (87 tickers + SPY benchmark)
- Period: 2017-01-01 → 2026-05-25
- Walk-forward: rolling
- Deployment gate (aggregate): Sharpe > 1.0 AND |DD| < 25.0% AND n ≥ 30
- Per-window clause: ≥ 50% of OOS windows with Sharpe > 0.5
- Param combinations evaluated: **3**

## Ranked combos

| Rank | Gate | OOS Sharpe | OOS DD | OOS n | OOS CAGR | Params (varied only) |
|---|---|---|---|---|---|---|
| 1 | ✅ | 1.60 | -24.19% | 1497 | +21.95% | bottom_n=5 |
| 2 | ❌ | 1.82 | -35.60% | 2428 | +33.37% | bottom_n=15 |
| 3 | ❌ | 1.81 | -37.28% | 2424 | +33.04% | bottom_n=10 |

## Top combo — full detail

Params:
- `lookback_days`: 5
- `rebalance_period_days`: 5
- `max_hold_days`: 5
- `atr_period`: 20
- `atr_stop_multiple`: 2.0
- `risk_per_trade`: 0.01
- `benchmark`: SPY
- `bottom_n`: 5

## OOS — DEPLOYMENT GATE

### Trade stats
- Trades: **1497** (wins 812 / losses 684 / breakeven 1)
- Win rate: **54.2%**
- Avg winner: +0.66R · Avg loser: -0.60R
- Expectancy / trade: **+0.08R**
- Profit factor: **1.30**
- Avg bars held: 4.7

### Return stats
- Sharpe (annualised): **1.60**
- Sortino: 3.36 · Calmar: 0.91
- Max drawdown: **-24.19%**
- Cumulative return: +228.39% (CAGR +21.95%)
- Deployment gate (Sharpe > 1.0 AND |DD| < 25% AND n ≥ 30): **✅ PASSED**

### Exit reasons
- max_hold: 1242
- stop_hit: 210
- gap_through_stop: 45

### Per-grade trade stats
- **B**: n=1497 · win_rate=54% · expectancy=+0.08R · PF=1.30

## Top combo — per-window OOS breakdown

| IS window | OOS window | OOS n | OOS Sharpe | OOS DD | Gate |
|---|---|---|---|---|---|
| 2017-01-01..2020-01-01 | 2020-01-01..2021-01-01 | 249 | 2.73 | -18.62% | ✅ |
| 2018-01-01..2021-01-01 | 2021-01-01..2022-01-01 | 250 | 1.18 | -8.14% | ✅ |
| 2019-01-01..2022-01-01 | 2022-01-01..2023-01-01 | 249 | -1.43 | -18.91% | ❌ |
| 2020-01-01..2023-01-01 | 2023-01-01..2024-01-01 | 250 | 2.98 | -10.82% | ✅ |
| 2021-01-01..2024-01-01 | 2024-01-01..2025-01-01 | 249 | 2.09 | -8.15% | ✅ |
| 2022-01-01..2025-01-01 | 2025-01-01..2026-01-01 | 250 | 1.85 | -12.10% | ✅ |

## Top combo — tightened gate verdict

- Aggregate clause: Sharpe 1.60 · |MDD| 24.19% · n 1497 → **PASSED**
- Per-window clause: 5/6 windows above Sharpe 0.5 (pass rate 0.83, required 0.50) → **PASSED**
- **Composite gate: PASSED**

## Doctrine verdict

Per `swing-risk-compliance-doctrine` + `walk-forward-analysis`: a strategy ships to paper-trade (then live) only when (a) the **aggregated OOS** clears `spec.gate.sharpe_min` / `spec.gate.max_dd_pct` / `spec.gate.n_min`, AND (b) for rolling walk-forward, at least `spec.gate.min_window_pass_rate` of the per-window OOS reports individually clear Sharpe > `spec.gate.min_window_sharpe`. Gate-passing combos are deployable candidates; gate-failing combos are research material.