# `tools/quant_strategies/` — Quant-strategist's strategy library

Strategies are declared as YAML specs and consumed by the
`quant-strategist` subagent + `tools.quant_strategies.runner`. Each spec
names a `kind` that resolves to a Python plugin under `_kinds/`
implementing the per-bar logic. Per `[[auto-research-loop]]`: the YAML
is the editable file, the runner is the immutable `prepare.py`, the
deployment gate (Sharpe > 1.0 AND |DD| < 25% AND n ≥ 30 OOS) is the
promotion filter.

## Layout

```
tools/quant_strategies/
├── README.md                       — this file
├── __init__.py
├── runner.py                       — spec loader + grid + ranked report
├── clenow_momentum.yml             — v1 reference strategy (multi-week momentum)
└── _kinds/
    ├── __init__.py                 — KIND_REGISTRY
    └── clenow_momentum.py          — Clenow Stocks-on-the-Move plugin
```

## Spec schema

```yaml
meta:
  name: <human-readable name>
  version: "1.0"
  source: <citation — paper, book, blog post>
  description: <one-paragraph>
  horizon_days: <int target hold>
  status: <v1_reference | candidate | retired>

kind: <KIND_REGISTRY key — e.g. "clenow_momentum">

universe:
  tickers: [<ticker>, ...]
  benchmark: <ticker — included in fetches; cross-sectional ranker
              excludes it from candidate pool; regime filter uses it>

period:
  start: "YYYY-MM-DD"
  end: "YYYY-MM-DD"

walk_forward:
  mode: single | rolling
  is_fraction: 0.70                  # single mode only
  is_years: 3                        # rolling mode
  oos_years: 1                       # rolling mode
  step_years: 1                      # rolling mode

params:
  # Kind-specific params. Scalar values are fixed; list values trigger
  # cartesian-product grid search.
  some_param: 90
  some_grid_param: [60, 90, 120]
  risk_per_trade: 0.01               # equity risk per trade (Sharpe / DD calc)

execution:
  trail: fixed | ratchet | ma_trail  # Phase 5.b stop-trail policy
  trail_ma_period: 10                # ma_trail mode only

gate:
  sharpe_min: 1.0
  max_dd_pct: 25.0
  n_min: 30
```

## Kind plugin contract

A kind module under `_kinds/` exposes:

```python
KIND: str                         # registry key, e.g. "clenow_momentum"

def precompute(
    universe_dfs: dict[str, pd.DataFrame],
    params: dict,
) -> object | None:
    """Cross-sectional pre-pass. Return None if not needed."""

def replay(
    df: pd.DataFrame,           # one ticker's OHLCV
    ticker: str,
    params: dict,
    state: object | None,       # whatever precompute returned
) -> list[TradeSignal]:
    """Per-ticker signal emission."""
```

Register the module in `_kinds/__init__.py`'s `KIND_REGISTRY`.

The `TradeSignal` dataclass is the existing
`tools.backtest.setup_replay.TradeSignal` — quant strategies are
siblings of the discretionary setups in the simulator + metrics
pipeline. The runner passes signals + post-entry OHLCV to
`tools.backtest.simulator.simulate_signals()`, then composes
`metrics.evaluate()` and `walk_forward.split_trades_by_window()` for
the gate verdict.

## CLI

```bash
uv run python -m tools.quant_strategies.runner \
    --spec tools/quant_strategies/clenow_momentum.yml \
    --out backtest_results/clenow_momentum.md
```

Stdout: same Markdown the file gets. Cartesian product of any list-
valued params is run; the report ranks combos by (gate-passed, OOS
Sharpe).

## v1 caveats

- **TradeSignal compatibility shim** for portfolio-ranking strategies
  like Clenow. Faithful continuous-hold semantics (one trade until
  ticker exits top-K) need a portfolio simulator with concurrent
  positions + cash management — Phase 5.d work. v1 ships the shim
  (re-entries each rebalance if still in top-K) to validate the
  quant-strategist architecture end-to-end.
- **No transaction-cost / slippage modeling.** Phase 5.a backtest
  pipeline assumes zero costs. Discount any borderline gate-passing
  result by 0.05-0.15R as a rule of thumb (per
  `tools/backtest/README.md` known limitations).
- **No factor-exposure check.** A passing strategy could be hidden
  beta to size / value / momentum / quality factors. Phase v2 work
  (cite `[[walk-forward-analysis]]` discipline lineage + López de
  Prado for the methodology).
- **No multi-strategy portfolio construction.** Each strategy is
  evaluated standalone. Combining multiple gate-passers into a
  portfolio (equal-weight, risk-weighted, Kelly-fractional) is a
  separate research surface.

## Related

- `[[swing-quant-research]]` (vault) — research stub that motivated this build
- `[[auto-research-loop]]` (vault) — the architectural pattern
- `[[walk-forward-analysis]]` (vault) — the discipline lineage
  underpinning the deployment gate
- `tools/backtest/README.md` — Phase 5 backtest pipeline this layers on
- `.claude/agents/quant-strategist.md` — the subagent that drives this
- `CLAUDE.md` § Quant dimension — full architectural framing
