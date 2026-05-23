"""Phase 5 — walk-forward backtest harness.

Per ``swing-risk-compliance-doctrine.md`` Phase 5 + the operational notes'
``Walk-forward validation REQUIRED before any setup ships to live workflow``
rule. Gates new-setup deployment on **out-of-sample Sharpe > 1.0 and max
drawdown < 25%** over a 5+ year backtest.

Modules:

* :mod:`tools.backtest.data_cache` — yfinance fetch + on-disk parquet cache
* :mod:`tools.backtest.setup_replay` — walk historical OHLCV day-by-day,
  fire Phase 2 setup detectors as of each bar
* :mod:`tools.backtest.simulator` — trade-signal → outcome (stop/target/max-hold)
* :mod:`tools.backtest.metrics` — Sharpe, Sortino, Calmar, max drawdown,
  R-multiple distribution, per-setup breakdown
* :mod:`tools.backtest.walk_forward` — IS/OOS windowing
* :mod:`tools.backtest.runner` — CLI orchestrator
"""
__version__ = "0.1.0"
