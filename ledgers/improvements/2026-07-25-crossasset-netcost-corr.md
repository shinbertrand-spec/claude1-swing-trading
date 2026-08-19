# Cross-asset trend (Phase 0) — net-of-cost + OOS corr to ts_momentum

## Net-of-cost gate (hardened portfolio sim)

| variant | net FULL Sharpe | full MDD% | full n | net OOS Sharpe | OOS MDD% | OOS n | net verdict |
|---|---|---|---|---|---|---|---|
| ts_momentum_liquid_us (baseline) | 1.36 | -16.9 | 736 | 1.04 | -13.7 | 237 | KEEP |
| crossasset_trend lb=126 | 0.75 | -39.6 | 791 | 0.52 | -39.6 | 264 | RETIRE |
| crossasset_trend lb=252 | 0.69 | -39.3 | 722 | 0.48 | -33.6 | 254 | RETIRE |

## OOS-window return-stream correlation to ts_momentum_liquid_us

| variant | OOS start | overlap days | Pearson r vs ts_momentum |
|---|---|---|---|
| crossasset_trend lb=126 | 2021-06-23 | 1235 | -0.003 |
| crossasset_trend lb=252 | 2021-06-23 | 1235 | -0.035 |
# SUPERSEDED by v2 — see note in combined-book.md
