"""Quant-strategist subagent's strategy library.

Strategies are declared as YAML specs at ``tools/quant_strategies/<NAME>.yml``
and consumed by :mod:`tools.quant_strategies.runner`. Each spec names a
``kind`` that resolves to a Python module under :mod:`tools.quant_strategies._kinds`
implementing the per-bar logic.

The kind contract is two functions:

* ``precompute(universe_dfs: dict[str, pd.DataFrame], params: dict) -> object``
  (optional; return None when no cross-sectional state is needed).
* ``replay(df, ticker, params, state) -> list[TradeSignal]`` — same shape as
  :data:`tools.backtest.setup_replay.SETUP_REPLAY_REGISTRY` replay functions
  but with an extra ``state`` parameter the runner threads through from
  precompute.

The runner ties precompute + per-ticker replay together, then hands the
emitted signals + post-entry OHLCV to :mod:`tools.backtest.simulator` /
:mod:`tools.backtest.metrics` / :mod:`tools.backtest.walk_forward` for the
deployment-gate verdict.

Per [[auto-research-loop]]: the strategy YAML is the editable file, the
backtest pipeline is the immutable ``prepare.py``, the deployment gate
(Sharpe > 1.0 AND |DD| < 25% AND n >= 30 OOS) is the promotion filter.
"""
