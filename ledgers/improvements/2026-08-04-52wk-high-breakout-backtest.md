# Strategy #2 — 52-week-high breakout — net-of-cost walk-forward gate (pilot, 2026-08-04)

Universe sp500_leaning_88 (+SPY). 2017-01-01..2026-05-25. Params {'lookback_high': 252, 'cooldown_days': 21, 'atr_period': 20, 'atr_stop_multiple': 3.0, 'max_hold_days': 60}.
Net-of-cost: marketable-limit + full spread + OHLC fills + 8-cap; rolling 3y-IS/1y-OOS/1y-step.

- Signals: **2058**
- Aggregate OOS: Sharpe **0.41** · |MDD| 13.2% · n 282 · fill 19%
- Aggregate gate: **FAIL**
- Per-window: 1/6 clear Sharpe>0.5 (rate 17%) -> **FAIL**
  - windows: 2020:0.28x 2021:-0.06x 2022:-0.30x 2023:0.42x 2024:0.89v 2025:0.05x
- Deflated Sharpe (agg OOS curve): **0.17** (gate >0.95)

## Verdict: **FAIL — does not clear the net-of-cost gate**
- Suggest-only; nothing applied. If PASS, broaden to liquid_us_2026q2 + run blind judge/critic before any operator merge.
