# ts_momentum_liquid_us — lookback_days sweep through the realistic fill-recert harness (pilot, 2026-07-02)

Same LIVE execution model as scripts/momentum_fill_recert.py (marketable limit prior_close x1.03, FULL effective spread, OHLC fills, 8-concurrent cap, rolling 3y-IS/1y-OOS/1y-step walk-forward). Gate: Sharpe>1.0 & |MDD|<25% & n>=30 on FULL & OOS-aggregate, AND >=50% of OOS windows clear Sharpe>0.5. Only lookback_days varies.
Deployed config is lookback_days=252.

| lookback | n_sig | FULL S | FULL MDD% | FULL n | fill% | OOS-agg S | per-window (clears/total) | weakest pass | VERDICT |
|---|---|---|---|---|---|---|---|---|---|
| 126 | 856 | 0.69 | 41.6 | 781 | 92 | 0.58 | 1/6 (2020:0.58✓ 2021:0.31✗ 2022:0.09✗ 2023:0.07✗ 2024:0.18✗ 2025:0.49✗) | 0.58 | **RETIRE / REBUILD** |
| 252  ← deployed | 808 | 1.35 | 16.9 | 736 | 92 | 1.23 | 3/6 (2020:0.64✓ 2021:0.54✓ 2022:0.29✗ 2023:0.41✗ 2024:0.63✓ 2025:0.50✗) | 0.54 | **DEPLOY — MARGINAL/FRAGILE** |

Interpretation: a lookback whose per-window clears/total is HIGHER (and whose weakest passing window sits further above the 0.5 floor) is the more robust config. If neither beats 3/6-at-the-floor, the fragility is a property of the ts_momentum signal on this universe, not of the lookback choice, and the deployed 252 stands as-is.
