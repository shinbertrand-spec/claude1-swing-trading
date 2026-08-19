# Cross-asset trend + synthetic shorts (inverse ETFs) — decorrelation + combined book

- Common window: **2021-06-23 → 2026-05-25** (1235 weekday days)
- Universe: 8 long diversifiers + 7 synthetic-short (-1x inverse) ETFs, crypto-free
- **OOS correlation ts vs cross-asset L/S: r = +0.063**
  (vs long-only crypto-free r=+0.205 — did adding the short side lower it?)

| book | ann. Sharpe | MDD% | days |
|---|---|---|---|
| ts_momentum ALONE | 1.06 | -17.3 | 1235 |
| cross-asset L/S ALONE | 0.18 | -10.7 | 1235 |
| 50/50 equal blend | 1.06 | -10.0 | 1235 |
| inverse-vol (0.20 ts / 0.80 ls) | 0.85 | -8.0 | 1235 |
| 70/30 (ts-heavy) | 1.07 | -12.9 | 1235 |

**Read:** the short side's value = (a) a LOWER (ideally negative) correlation to ts_momentum than the long-only +0.205, and (b) a combined book that beats ts-alone on Sharpe or MDD. Synthetic shorts carry inverse-ETF decay (embedded in price) + net-cost. No look-ahead in 50/50, 70/30, inverse-vol.
