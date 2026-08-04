# Strategy #3 — NR7 contraction breakout — net-of-cost walk-forward gate (pilot, 2026-08-04)

Universe sp500_leaning_88 (+SPY). 2017-01-01..2026-05-25. Params {'nr_window': 7, 'atr_period': 20, 'atr_stop_multiple': 2.5, 'max_hold_days': 20, 'cooldown_days': 5}.

- Signals: **14178**
- Aggregate OOS: Sharpe **1.00** · |MDD| 11.1% · n 746 · fill 8%
- Aggregate gate: **FAIL** · Per-window 4/6 -> **PASS** · DSR **0.79**
  - windows: 2020:0.44x 2021:0.65v 2022:0.04x 2023:0.87v 2024:0.53v 2025:0.66v

## Verdict: **FAIL — does not clear the net-of-cost gate**
- Suggest-only; nothing applied.
