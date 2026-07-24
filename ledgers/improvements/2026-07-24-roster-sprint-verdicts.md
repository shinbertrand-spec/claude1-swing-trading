# Roster-replenishment sprint verdicts (Phase 1b, 2026-07-24)

Full stack: zero-cost grid gate (runner) + C1 DSR (report-only) +
net-of-cost 6-window walk-forward + measured correlation vs
ts_momentum_liquid_us. Grids exactly as spec files declare. Promotion =
operator's call; pre-registered expectation 0-1 of 3 pass.

| candidate | grid gate (best combo) | DSR | net agg Sharpe | net windows | net gate | corr vs ts_mom | verdict |
|---|---|---|---|---|---|---|---|
| connors_rsi2 | FAIL | 0.846 | 0.28 | 0/6 | FAIL | 0.22 | **FAIL** |
| xs_low_volatility | FAIL | 0.341 | -0.03 | 0/2 | FAIL | 0.05 | **FAIL** |
| event_insider_buying | FAIL | n/a | 0.00 | 0/4 | FAIL | 0.34 | **FAIL** |

- Full grid reports: `journal/backtest/2026-07-24-sprint-<setup>.md`
- Next: re-derive the trial registry (`uv run python -m tools.backtest.dsr_gate derive --write`).
