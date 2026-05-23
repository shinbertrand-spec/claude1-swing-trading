# Phase 5 — Walk-Forward Backtest Harness

Per `swing-risk-compliance-doctrine` Phase 5 + the `walk-forward-analysis` vault concept page. The doctrine gates setup deployment to live capital on:

> **Out-of-sample Sharpe > 1.0 AND |max drawdown| < 25% AND OOS n ≥ 30 trades.**

If any of those fails on OOS data, the setup does not ship to live.

## Modules

| Module | Role |
|---|---|
| [`data_cache.py`](data_cache.py) | yfinance OHLCV fetch + on-disk parquet cache; CLI for fetch/info/clear |
| [`setup_replay.py`](setup_replay.py) | SEPA-VCP replay + central `SETUP_REPLAY_REGISTRY` (side-effect imports for the other setups) |
| [`ep_replay.py`](ep_replay.py) | EP setup replay — gap+volume catalyst-signature approximation of MAGNA (fundamentals omitted in Phase 5.b) |
| [`pullback_replay.py`](pullback_replay.py) | Secondary 1 — pullback to 20-day SMA |
| [`rsi_div_replay.py`](rsi_div_replay.py) | Secondary 2 — RSI(14) bullish divergence at support |
| [`resistance_break_replay.py`](resistance_break_replay.py) | Secondary 3 — non-VCP resistance breakout with volume |
| [`trailing_stop.py`](trailing_stop.py) | Stop-trail policies: `fixed` / `ratchet` (per swing-position-sizing) / `ma_trail` (Kullamägi) |
| [`simulator.py`](simulator.py) | `TradeSignal` + post-entry OHLCV → `TradeOutcome`; per-bar evaluation with selectable trail policy + optional sell-aware exits |
| [`sell_aware.py`](sell_aware.py) | Per-bar sell-discipline composer (climax-top + violations + base-stage + sell-into-strength → `sell_decision`); enables non-stop exits |
| [`pyramid_simulator.py`](pyramid_simulator.py) | Anchor-and-Pyramid multi-leg simulator; STARTER + Momentum-Burst ADD-ON #1 + Day-7-milestone ADD-ON #2; combined-BE stop migration |
| [`metrics.py`](metrics.py) | Sharpe / Sortino / Calmar / max drawdown / win rate / profit factor / expectancy / per-grade breakdown + deployment-gate verdict |
| [`walk_forward.py`](walk_forward.py) | IS/OOS windowing — single-split + rolling-splits helpers; trade partitioning by `fill_date` |
| [`runner.py`](runner.py) | End-to-end orchestrator + CLI; emits a Markdown report; single-split OR rolling walk-forward; `--pyramid` + `--sell-aware` toggles |

## Quick start

```powershell
# Fetch 5y of OHLCV for a universe (cached to tools/backtest/cache/)
uv run python -m tools.backtest.data_cache fetch AAPL MSFT NVDA GOOGL META SPY QQQ XLK

# Inspect cache
uv run python -m tools.backtest.data_cache info AAPL

# Run a full SEPA-VCP backtest 2020–2025, IS/OOS 70/30, ratchet trail
uv run python -m tools.backtest.runner \
    --setup SEPA-VCP \
    --tickers AAPL,MSFT,NVDA,GOOGL,META,SPY,QQQ,XLK \
    --start 2020-01-01 --end 2025-12-31 \
    --is-fraction 0.70 \
    --trail ratchet \
    --max-hold-days 30 \
    --target-r-multiple 2.0 \
    --risk-per-trade 0.01

# EP setup, MA-trail (Kullamägi 10-day MA), 60-bar hold, 3R target
uv run python -m tools.backtest.runner \
    --setup EP \
    --tickers AAPL,MSFT,NVDA,GOOGL,SMCI,META,AMD,TSLA,AVGO \
    --start 2020-01-01 --end 2025-12-31 \
    --trail ma_trail --trail-ma-period 10 \
    --max-hold-days 60 \
    --target-r-multiple 3.0

# Rolling walk-forward: 3y IS + 1y OOS, step 1y, ratchet trail
uv run python -m tools.backtest.runner \
    --setup SEPA-VCP \
    --tickers SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,META \
    --start 2015-01-01 --end 2025-01-01 \
    --rolling --is-years 3 --oos-years 1 --step-years 1 \
    --trail ratchet

# Phase 5.c — Anchor-and-Pyramid (SEPA-VCP, addon-1 in first 10 bars,
# addon-2 on Day-7 milestone for Super Swan / Golden EP grades)
uv run python -m tools.backtest.runner \
    --setup SEPA-VCP \
    --tickers SPY,QQQ,AAPL,MSFT,NVDA,GOOGL \
    --start 2020-01-01 --end 2025-12-31 \
    --pyramid --regime-class stage_2_confirmed --trail ratchet

# Phase 5.c — Sell-aware exits (per-bar sell_decision composer + ratchet trail)
uv run python -m tools.backtest.runner \
    --setup EP \
    --tickers SMCI,NVDA,AMD,AVGO,META,AAPL \
    --start 2020-01-01 --end 2025-12-31 \
    --sell-aware --sell-grace-period 5 --trail ratchet \
    --max-hold-days 60 --target-r-multiple 3.0
```

The runner prints a Markdown report with full / IS / OOS metrics plus the doctrine's deployment-gate verdict.

## What's shipped vs deferred

**Phase 5.a + 5.b + 5.c — five setups, three trail modes, pyramiding, sell-aware exits, single + rolling walk-forward:**

* Setups: SEPA-VCP, EP, Pullback-20SMA, RSI-Divergence, Resistance-Breakout
* Trail policies: `fixed` (Phase 5.a baseline), `ratchet` (trail-to-BE at +5%, trail-to-+5% at +10% per swing-position-sizing), `ma_trail` (Kullamägi N-day SMA, exits on close-below)
* **Pyramiding (Phase 5.c):** `pyramid_simulator` walks bars and adds legs mid-trade — Momentum Burst within window triggers ADD-ON #1 (brings to full size, stop migrates to combined break-even); Day-7 milestone gates ADD-ON #2 by grade (Super Swan / Golden EP only) + regime
* **Sell-aware exits (Phase 5.c):** `sell_aware` runs the 4 OHLCV-derivable sell-discipline detectors per bar (climax-top patterns, violations, base stage, sell-into-strength) and composes via `sell_decision`; non-hold action exits at the bar's close
* Per-trade simulation: gap-through-stop / stop / target / sell-decision / max-hold / end-of-data; trail-aware exit semantics
* Single 70/30 IS/OOS split OR rolling walk-forward (configurable IS years / OOS years / step years)
* Trade-level metrics: Sharpe, Sortino, Calmar, max drawdown, win rate, expectancy, profit factor, per-grade breakdown
* Aggregated deployment-gate verdict on OOS (rolling mode concatenates all OOS windows before evaluating)

**Phase 5.d (deferred):**

* **Portfolio-equity simulator** — concurrent positions + cash tracking + sector caps + per-day equity curve. Required for a realistic "could this account actually have taken all these signals" answer; current backtest assumes infinite capital.
* **Pyramid + sell-aware combined** — pyramid simulator currently exits only via stop/target/max-hold; per-bar `sell_decision` on a pyramided position is Phase 5.d (interacts with combined break-even semantics).
* **P/E expansion warning** — `pe_expansion_check` requires fundamentals history not in OHLCV. `sell_aware` passes `pe_expansion_warning=False` for now; lights up when a real fundamentals source (SimFin, Sharadar) lands.
* **Real fundamentals for EP MAGNA** — Phase 5.b approximates M (massive earnings) and A (analyst upgrades) from gap+volume signature; a real source lifts EP grading accuracy.
* **Sensitivity sweeps** — parameter grid over `atr_multiple`, `max_hold_days`, `target_r_multiple`, `addon_1_window`, etc.
* **HTML / PDF report generation.**

## Known limitations + biases

Per `walk-forward-analysis` vault page:

* **Survivorship bias** — yfinance ticker universe today excludes delisted names. Mitigation requires a delisted-securities database (Norgate, CRSP) which is paid. Phase 5.a accepts the bias and documents it.
* **Look-ahead bias** — guarded: replay slices `df[:i+1]` at bar `i`; signals fill on bar `i+1` open.
* **Overfitting / data-snooping** — guarded by the IS/OOS split. With Phase 5.a no parameter is tuned, so the split is structural rather than load-bearing — but the framework is there for Phase 5.b.
* **No transaction costs / slippage** — backtests assume fills at the exact stop / target / open. Real fills will be worse, especially on stop hits in fast markets. Discount the reported R-multiple by 0.05–0.15R if the strategy is borderline.

## Deployment gate

```python
deployment_gate_passed = (
    returns.sharpe_annualised > 1.0
    AND abs(returns.max_drawdown_pct) < 25.0
    AND trades.n_trades >= 30
)
```

If the OOS report's `deployment_gate_passed: false`, the setup does not ship to `risk-and-compliance` Mode 2 approval. Re-tune (Phase 5.b), expand the ticker universe, or shelve the setup.

## Tests

80 tests covering data_cache (no-network), setup_replay (synthetic OHLCV), simulator (engineered scenarios + trail modes + sell-aware integration), metrics (Sharpe / DD math on known returns), walk_forward (date windowing), trailing_stop (ratchet + MA-trail policy semantics), replays (registration + shape + insufficient-history for all 4 Phase 5.b setups), pyramid_simulator (ADD-ON #1/#2 firing + gating + chase check + combined R-multiple), sell_aware (composer on calm vs parabolic data + grace-period gating). Run with `uv run pytest tests/test_backtest_*.py`.

## Related

- `tools/README.md` — full tools catalog
- Vault: `wiki/concepts/walk-forward-analysis.md`
- Vault: `wiki/notes/swing-risk-compliance-doctrine.md` (Phase 5 motivation)
- Vault: each operational note ends with a "Walk-forward validation REQUIRED" callout
