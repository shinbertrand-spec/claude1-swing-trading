# xs_short_term_reversal — bottom_n sweep through the net-of-cost gate (pilot, 2026-07-01)

Same hardened net-of-cost simulator as scripts/net_gate_rerun.py (cost_model + OHLC fills + cap-weight), same OOS last-30% split, same spec gate (Sharpe>1.0 / |MDD|<25% / n>=30). Only bottom_n varies.
Deployed config is bottom_n=5 (first combo of the grid).

| bottom_n | n_sig | FULL net Sharpe | full MDD% | full n | fill% | full ret% | filled_fwd% | missed_fwd% | OOS Sharpe | OOS MDD% | OOS n | VERDICT |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 5  ← deployed | 2330 | 0.82 | -12.5 | 1894 | 81 | 79.3 | 0.19 | 3.99 | 0.50 | -9.3 | 573 | **RETIRE** |
| 10 | 4670 | 1.04 | -15.8 | 3362 | 72 | 181.5 | 0.28 | 3.52 | 0.67 | -13.3 | 1023 | **RETIRE** |
| 15 | 7007 | 1.08 | -14.1 | 3585 | 51 | 179.7 | 0.27 | 3.39 | 0.65 | -10.6 | 1085 | **RETIRE** |

Interpretation: if every row RETIREs, the net-of-cost failure is a property of the signal (adverse selection: limit orders fill the losers that keep falling, miss the winners that gap up), not of the bottom_n choice.
