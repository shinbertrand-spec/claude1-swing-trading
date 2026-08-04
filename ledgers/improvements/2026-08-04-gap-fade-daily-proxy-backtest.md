# Gap-fade DAILY PROXY (Stage 0) — net-of-cost gate (pilot, 2026-08-04)

Universe sp500_leaning_88 (87 loaded). 2017-01-01..2026-05-25. Trend filter (above 200d SMA): True. Buy down-gap OPEN, sell same-day CLOSE. Equal-weight across gapping names; flat when none gap. Gate: Sharpe>1.0 & |MDD|<25% & >=50% of 6 OOS yrs clear Sharpe>0.5 & DSR>0.95, NET.

**Optimistic upper bound: flat bps understates the wide gap-open spread. If this FAILS, the paid intraday feed cannot rescue it. If it PASSES, Stage 1 (paid feed, real entry timing + real spread) is justified.**

| gap% | cost bps | daily Sharpe | |MDD|% | cum% | OOS yrs>0.5 | DSR | GATE |
|---|---|---|---|---|---|---|---|
| 1 | 5 | 1.08 | 44.8 | 849 | 4/6 | 1.00 | FAIL |
| 1 | 10 | 0.73 | 51.0 | 313 | 4/6 | 0.97 | FAIL |
| 1 | 20 | 0.02 | 68.8 | -22 | 4/6 | 0.37 | FAIL |
| 2 | 5 | 1.06 | 45.9 | 791 | 4/6 | 1.00 | FAIL |
| 2 | 10 | 0.86 | 51.5 | 449 | 4/6 | 0.99 | FAIL |
| 2 | 20 | 0.44 | 61.5 | 108 | 4/6 | 0.83 | FAIL |
| 3 | 5 | 1.17 | 24.5 | 798 | 4/6 | 1.00 | PASS |
| 3 | 10 | 1.03 | 29.3 | 573 | 4/6 | 1.00 | FAIL |
| 3 | 20 | 0.75 | 38.1 | 277 | 4/6 | 0.97 | FAIL |

## Verdict: **AT LEAST ONE variant clears the gate -> Stage 1 (paid feed) JUSTIFIED**
- Trades/yr are episodic (fires only on down-gap days), so the full-series Sharpe (with flat days) is a conservative capital-efficiency read; even so, the gate is the gate.
- Suggest-only; nothing applied.
