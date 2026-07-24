# Auto-paper performance — regenerated with A2 baselines

- asof: 2026-07-24T08:01:58+00:00
- Closed trades: 1 | open: 2 | submitted: 0
- Realized Sharpe / Sortino / Max DD: 0.0 / 0.0 / 0.0%

## Realized vs backtest expectation

| Setup | n | Realized Sharpe | Backtest Sharpe | Realized DD | Backtest DD | Status |
|---|---|---|---|---|---|---|
| clenow_momentum_liquid_us | 0 | None | 1.47 | None | -21.03 | no_data |
| connors_rsi2 | 1 | 0.0 | 1.01 | 0.0 | -18.39 | warn |
| connors_rsi2_ai_broad | 0 | None | 1.02 | None | -5.11 | no_data |
| residual_momentum_liquid_us | 0 | None | 1.09 | None | -21.58 | no_data |
| ts_momentum_liquid_us | 0 | None | 2.13 | None | -11.66 | no_data |
| xs_short_term_reversal | 0 | None | 1.6 | None | -24.94 | no_data |
| xs_short_term_reversal_ai_broad | 0 | None | 1.4 | None | -22.04 | no_data |
| xs_short_term_reversal_ai_pure | 0 | None | 1.86 | None | -19.34 | no_data |
| xs_short_term_reversal_liquid_us | 0 | None | 1.35 | None | -16.91 | no_data |

## Baselines — same window (A2)

Window: 2026-06-02 -> 2026-06-06 (identical to realized-trade window)

| Baseline | Total return | Sharpe | Sortino | Max DD | n days | Note |
|---|---|---|---|---|---|---|
| spy_buy_hold | -2.9% | -10.27 | -11.57 | -2.9% | 4 |  |
| equal_weight_traded_buy_hold | 2.31% | 17.66 | - | -0.02% | 4 | 1 traded tickers, equal weight at window start |
| spy_ts_momentum_12_1 | -2.9% | -10.27 | -11.57 | -2.9% | 4 | long SPY iff trailing 12-1 return > 0, else cash |

## Notes

- AMD: closed ledger has no exit_price (closed-unfilled or Session 3 close-out path not yet merged); excluded from realized stats
- CAT: closed ledger has no exit_price (closed-unfilled or Session 3 close-out path not yet merged); excluded from realized stats
- GOOGL: closed ledger has no exit_price (closed-unfilled or Session 3 close-out path not yet merged); excluded from realized stats
- Sharpe / max-DD computed via trade-sequence equity curve (risk_per_trade=1%); see metrics._equity_curve. Bar-by-bar intra-trade drawdown is NOT captured — adequate at n>=30, noisier below.
