# Roster sprint — net-of-cost gate (promoted combo) + OOS corr to ts_momentum

## Net-of-cost gate (hardened portfolio sim, promoted combo)

| setup | combo | net FULL Sharpe | full MDD% | full n | net OOS Sharpe | OOS MDD% | OOS n | gate(S/DD/n) | net verdict |
|---|---|---|---|---|---|---|---|---|---|
| connors_rsi2 | {'entry_threshold': 15, 'cooldown_days': 5} | 0.30 | -13.0 | 1824 | 0.58 | -4.4 | 761 | 1.0/25/30 | RETIRE |
| xs_low_volatility | {'bottom_n': 20} | 0.68 | -3.6 | 378 | 0.18 | -3.6 | 146 | 1.0/25/30 | RETIRE |
| event_insider_buying | (fixed) | 0.12 | -25.4 | 208 | 0.23 | -7.0 | 58 | 1.0/25/30 | RETIRE |
| ts_momentum_liquid_us | {'lookback_days': 252, 'top_k': 8} | 1.36 | -16.9 | 736 | 1.04 | -13.7 | 237 | 1.0/25/30 | KEEP |

## OOS-window return-stream correlation to ts_momentum_liquid_us

Each candidate's FULL-period daily portfolio returns, sliced to its own OOS window (last-30% split), Pearson r vs ts_momentum over the intersection of trading dates.

| candidate | OOS start | overlap days | Pearson r vs ts_momentum |
|---|---|---|---|
| connors_rsi2 | 2023-07-30 | 707 | +0.264 |
| xs_low_volatility | 2024-03-13 | 452 | +0.004 |
| event_insider_buying | 2022-08-07 | 603 | +0.294 |
