# Strategy #3b — NR7 contraction breakout (liquid_us + top-8 rank) — net-of-cost gate (pilot, 2026-08-04)

Universe liquid_us_2026q2 (~1178 +SPY). 2017-01-01..2026-05-25. Params {'nr_window': 7, 'atr_period': 20, 'atr_stop_multiple': 2.5, 'max_hold_days': 20, 'cooldown_days': 5, 'rank_lookback': 63, 'top_k': 8}.
Ranked: top-8 per signal date by trailing 63d return (trend-confirmed contraction).

- Raw signals -> ranked: see stdout. Aggregate OOS: Sharpe **0.95** · |MDD| 13.5% · n 793 · fill 7%
- Aggregate gate **FAIL** · Per-window 1/6 -> **FAIL** · DSR **0.69**
  - windows: 2020:0.49x 2021:0.40x 2022:-0.23x 2023:0.42x 2024:0.65v 2025:0.44x

## Verdict: **FAIL — does not clear the net-of-cost gate**
- Suggest-only; nothing applied.
