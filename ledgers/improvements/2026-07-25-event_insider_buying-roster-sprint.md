# Quant-strategist run — event_insider_buying

- Kind: **event_insider_buying**
- Source: Cohen-Malloy-Pomorski 2012 â€” opportunistic insider buying / post-event drift
- Universe: liquid_us_2026q2 (1178 tickers + SPY benchmark)
- Period: 2017-01-01 → 2024-12-31
- Walk-forward: rolling
- Deployment gate (aggregate): Sharpe > 1.0 AND |DD| < 25.0% AND n ≥ 30
- Per-window clause: ≥ 50% of OOS windows with Sharpe > 0.5
- Param combinations evaluated: **1**

## Ranked combos

| Rank | Gate | OOS Sharpe | OOS DD | OOS n | OOS CAGR | Params (varied only) |
|---|---|---|---|---|---|---|
| 1 | ❌ | 0.23 | -22.91% | 117 | +1.77% | _(all fixed)_ |

## Top combo — full detail

Params:
- `events_path`: ledgers/insider/events/liquid_us_2026q2.yml
- `min_conviction`: medium
- `max_hold_days`: 126
- `atr_period`: 20
- `atr_stop_multiple`: 3.0
- `benchmark`: SPY

## OOS — DEPLOYMENT GATE

### Trade stats
- Trades: **117** (wins 40 / losses 77 / breakeven 0)
- Win rate: **34.2%**
- Avg winner: +2.21R · Avg loser: -1.03R
- Expectancy / trade: **+0.08R**
- Profit factor: **1.12**
- Avg bars held: 66.6

### Return stats
- Sharpe (annualised): **0.23**
- Sortino: 2.35 · Calmar: 0.08
- Max drawdown: **-22.91%**
- Cumulative return: +7.77% (CAGR +1.77%)
- Deployment gate (Sharpe > 1.0 AND |DD| < 25% AND n ≥ 30): **❌ FAILED**
- _Note: concurrent-position cap (max_concurrent=8) rejected 372/489 outcomes; stats reflect the 117 that would have been on book_

### Exit reasons
- stop_hit: 60
- max_hold: 43
- gap_through_stop: 14

### Per-grade trade stats
- **A**: n=20 · win_rate=40% · expectancy=+0.12R · PF=1.20
- **B**: n=97 · win_rate=33% · expectancy=+0.07R · PF=1.10

## Top combo — per-window OOS breakdown

| IS window | OOS window | OOS n | OOS Sharpe | OOS DD | Gate |
|---|---|---|---|---|---|
| 2017-01-01..2020-01-01 | 2020-01-01..2021-01-01 | 44 | 0.41 | -22.91% | ❌ |
| 2018-01-01..2021-01-01 | 2021-01-01..2022-01-01 | 24 | -0.08 | -6.98% | ❌ |
| 2019-01-01..2022-01-01 | 2022-01-01..2023-01-01 | 29 | 0.13 | -10.57% | ❌ |
| 2020-01-01..2023-01-01 | 2023-01-01..2024-01-01 | 27 | 0.58 | -7.81% | ❌ |

## Top combo — tightened gate verdict

- Aggregate clause: Sharpe 0.23 · |MDD| 22.91% · n 117 → **FAILED**
- Per-window clause: 1/4 windows above Sharpe 0.5 (pass rate 0.25, required 0.50) → **FAILED**
- **Composite gate: FAILED**
- _Note: aggregate fails: Sharpe 0.23 <= 1.0 | only 1/4 windows have Sharpe > 0.5 (pass rate 0.25 < 0.5)_

## Doctrine verdict

Per `swing-risk-compliance-doctrine` + `walk-forward-analysis`: a strategy ships to paper-trade (then live) only when (a) the **aggregated OOS** clears `spec.gate.sharpe_min` / `spec.gate.max_dd_pct` / `spec.gate.n_min`, AND (b) for rolling walk-forward, at least `spec.gate.min_window_pass_rate` of the per-window OOS reports individually clear Sharpe > `spec.gate.min_window_sharpe`. Gate-passing combos are deployable candidates; gate-failing combos are research material.
## 7th gate — Deflated Sharpe (C1)

- not computed (grid has < 2 combos — no trial variance)
