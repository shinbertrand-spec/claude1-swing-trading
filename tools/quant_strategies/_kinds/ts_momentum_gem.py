"""Time-Series Momentum + a Global-Equity-Momentum (Antonacci) crash switch.

ts_momentum already carries PER-NAME absolute momentum (its TSMOM sign gate only
holds names with positive trailing return). What it lacks — and what Antonacci's
Dual / Global Equity Momentum adds — is a MARKET-LEVEL absolute-momentum switch:
late in a cycle, individual leaders can still print positive 6-12mo trailing
returns right up to a market top, so per-name momentum keeps the book fully
invested into the drawdown. The GEM overlay overrides that.

Mechanism (single added rule, nothing else changes vs ts_momentum):
  * On each rebalance date, compute the BENCHMARK's (SPY) trailing
    ``regime_lookback_days`` return.
  * If it is <= 0 (risk-OFF), hold NOTHING that period — the entire held-set is
    cleared (a long-only proxy for Antonacci's rotate-to-bonds; the sim simply
    sits in cash).
  * If it is > 0 (risk-ON), the held-set is exactly ts_momentum's banded top-K.

This is deliberately the SAME selection as ts_momentum so a gate A/B isolates one
variable: the market-regime switch. Target metric is |max drawdown| — the line
that retired clenow_momentum (|MDD| 25.87% > 25%).

DOCTRINE NOTE: a market-timing switch is arguably more than the "param tuning +
universe swap" that the CLAUDE.md Alfred-Delta-5 guardrail permits without
re-review. It reuses the momentum family (absolute momentum on the benchmark
rather than on a name), but it is a new RULE. Treat any pass here as evidence to
open a doctrine conversation, not as an auto-deploy.
"""
from __future__ import annotations

import pandas as pd

from . import ts_momentum as tsm
from .ts_momentum import CrossSectionalState, _trailing_return_at  # reuse

KIND = "ts_momentum_gem"

DEFAULT_REGIME_LOOKBACK = 252  # 12-month absolute momentum (Antonacci GEM standard)


def precompute(universe_dfs: dict[str, pd.DataFrame], params: dict) -> CrossSectionalState:
    """ts_momentum's top-K state, with risk-OFF rebalance dates emptied."""
    state = tsm.precompute(universe_dfs, params)
    if not params.get("market_regime_gate", True):
        return state  # gate disabled -> identical to plain ts_momentum (the A/B baseline)

    benchmark = params.get("benchmark")
    bench_df = universe_dfs.get(benchmark) if benchmark else None
    if bench_df is None:
        return state  # no benchmark series -> can't gate; fall back to ungated

    regime_lb = int(params.get("regime_lookback_days", DEFAULT_REGIME_LOOKBACK))
    ranks, scores = {}, {}
    for d in state.rebalance_dates:
        tr = _trailing_return_at(bench_df, d, regime_lb)
        if tr is not None and tr <= 0:          # risk-OFF: hold nothing
            ranks[d], scores[d] = set(), {}
        else:                                    # risk-ON (or unknown early bars): as ts_momentum
            ranks[d] = state.ranks_by_date.get(d, set())
            scores[d] = state.score_by_date.get(d, {})
    return CrossSectionalState(ranks, scores, state.rebalance_dates)


def replay(df, ticker, params, state):
    """Identical to ts_momentum — only the precomputed held-set differs."""
    return tsm.replay(df, ticker, params, state)
