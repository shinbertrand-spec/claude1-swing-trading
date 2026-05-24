"""Quant-strategist runner.

Loads a strategy YAML spec, expands its parameter grid, runs each
combination through the Phase 5.a-c backtest pipeline (with cross-
sectional precompute when the kind provides it), and emits a ranked
Markdown report with the deployment-gate verdict for each combo.

Per [[auto-research-loop]]: the spec is the editable file, this runner
is the immutable ``prepare.py``, the deployment gate is the promotion
filter.

CLI::

    uv run python -m tools.quant_strategies.runner \\
        --spec tools/quant_strategies/clenow_momentum.yml \\
        --out backtest_results/clenow_momentum.md

Library::

    from tools.quant_strategies.runner import run_spec
    report = run_spec(spec_path)
    print(report["markdown"])
"""
from __future__ import annotations

import argparse
import itertools
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ..backtest import data_cache, metrics, simulator, walk_forward
from ..backtest.runner import _format_report
from ..backtest.trailing_stop import TrailConfig
from ._kinds import KIND_REGISTRY


def _expand_grid(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Cartesian-product expansion of any list-valued params.

    Scalar values pass through unchanged; list values become axes.
    """
    axes = {}
    fixed = {}
    for k, v in params.items():
        if isinstance(v, list):
            axes[k] = v
        else:
            fixed[k] = v
    if not axes:
        return [dict(fixed)]
    keys = list(axes.keys())
    values_lists = [axes[k] for k in keys]
    combos: list[dict[str, Any]] = []
    for combo in itertools.product(*values_lists):
        d = dict(fixed)
        d.update(dict(zip(keys, combo)))
        combos.append(d)
    return combos


def _load_universe(
    tickers: list[str],
    start: date,
    end: date,
    force_refetch: bool,
) -> dict[str, "pd.DataFrame"]:
    """Fetch + load every ticker in the universe; return {ticker: trimmed_df}."""
    import pandas as pd  # noqa: F401  (yfinance brings pandas; explicit ref for type clarity)

    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            data_cache.fetch(t, start=start, end=end, force_refetch=force_refetch)
        except Exception as exc:
            print(f"# {t}: fetch failed — {exc}", flush=True)
            continue
        df = data_cache.load(t)
        df = walk_forward.trim_ohlcv(df, start, end)
        if len(df) > 0:
            out[t] = df
    return out


def _run_one_combo(
    spec: dict[str, Any],
    params: dict[str, Any],
    universe_dfs: dict,
    kind_mod,
) -> dict[str, Any]:
    """Run a single param-combo end-to-end: precompute → replay → simulator → metrics."""
    benchmark = params.get("benchmark") or spec["universe"]["benchmark"]
    # Precompute (cross-sectional state for portfolio-ranking strategies).
    if hasattr(kind_mod, "precompute"):
        state = kind_mod.precompute(universe_dfs, params)
    else:
        state = None

    # Per-ticker replay → signals.
    all_signals = []
    all_outcomes = []
    per_ticker: dict[str, int] = {}
    trail_cfg = TrailConfig(
        mode=spec.get("execution", {}).get("trail", "fixed"),
        ma_period=int(spec.get("execution", {}).get("trail_ma_period", 10)),
    )
    for t, df in universe_dfs.items():
        if t == benchmark:
            per_ticker[t] = 0
            continue
        signals = kind_mod.replay(df, t, params, state)
        if not signals:
            per_ticker[t] = 0
            continue
        outcomes = simulator.simulate_signals(signals, df, trail_config=trail_cfg)
        all_signals.extend(signals)
        all_outcomes.extend(outcomes)
        per_ticker[t] = len(outcomes)

    # Sort by fill_date (matches existing runner convention).
    pairs = sorted(zip(all_signals, all_outcomes), key=lambda p: p[0].fill_date)
    all_signals = [p[0] for p in pairs]
    all_outcomes = [p[1] for p in pairs]

    # Walk-forward split.
    wf = spec.get("walk_forward", {"mode": "single", "is_fraction": 0.70})
    start_d = date.fromisoformat(spec["period"]["start"])
    end_d = date.fromisoformat(spec["period"]["end"])
    risk = float(params.get("risk_per_trade", 0.01))

    if wf.get("mode") == "rolling":
        specs = walk_forward.rolling_splits(
            start=start_d, end=end_d,
            is_years=int(wf.get("is_years", 3)),
            oos_years=int(wf.get("oos_years", 1)),
            step_years=int(wf.get("step_years", 1)),
        )
        concat_oos = []
        windows = []
        for s in specs:
            _is_sigs, is_outs, _oos_sigs, oos_outs = walk_forward.split_trades_by_window(
                all_signals, all_outcomes, s
            )
            oos_r = metrics.evaluate(oos_outs, risk_per_trade=risk)
            is_r = metrics.evaluate(is_outs, risk_per_trade=risk)
            windows.append({
                "spec": asdict(s),
                "is_n": is_r.trades.n_trades,
                "oos_n": oos_r.trades.n_trades,
                "oos_sharpe": oos_r.returns.sharpe_annualised,
                "oos_max_dd_pct": oos_r.returns.max_drawdown_pct,
                "oos_gate": oos_r.deployment_gate_passed,
            })
            concat_oos.extend(oos_outs)
        oos_report = metrics.evaluate(concat_oos, risk_per_trade=risk)
        return {
            "params": params,
            "oos_report": oos_report,
            "windows": windows,
            "per_ticker": per_ticker,
            "n_signals": len(all_signals),
        }
    else:
        is_fraction = float(wf.get("is_fraction", 0.70))
        s = walk_forward.single_split(start=start_d, end=end_d, is_fraction=is_fraction)
        _is_sigs, _is_outs, _oos_sigs, oos_outs = walk_forward.split_trades_by_window(
            all_signals, all_outcomes, s
        )
        oos_report = metrics.evaluate(oos_outs, risk_per_trade=risk)
        return {
            "params": params,
            "oos_report": oos_report,
            "split": asdict(s),
            "per_ticker": per_ticker,
            "n_signals": len(all_signals),
        }


def run_spec(spec_path: str | Path, force_refetch: bool = False) -> dict[str, Any]:
    """Load YAML spec, expand grid, run every combo, return ranked report."""
    spec_path = Path(spec_path)
    spec = yaml.safe_load(spec_path.read_text())
    kind_name = spec["kind"]
    if kind_name not in KIND_REGISTRY:
        raise ValueError(f"unknown kind {kind_name!r}; known: {sorted(KIND_REGISTRY)}")
    kind_mod = KIND_REGISTRY[kind_name]

    # Universe + period.
    tickers = list(spec["universe"]["tickers"])
    benchmark = spec["universe"]["benchmark"]
    if benchmark not in tickers:
        tickers.append(benchmark)
    start_d = date.fromisoformat(spec["period"]["start"])
    end_d = date.fromisoformat(spec["period"]["end"])
    universe_dfs = _load_universe(tickers, start_d, end_d, force_refetch)

    # Param-grid expansion. Inject benchmark into every combo for kind's use.
    raw_params = dict(spec.get("params", {}))
    raw_params.setdefault("benchmark", benchmark)
    combos = _expand_grid(raw_params)

    # Run each combo.
    combo_results = []
    for i, params in enumerate(combos, 1):
        print(f"# Combo {i}/{len(combos)}: {params}", flush=True)
        result = _run_one_combo(spec, params, universe_dfs, kind_mod)
        combo_results.append(result)

    # Apply deployment-gate thresholds from spec (defaults to doctrine).
    gate = spec.get("gate", {})
    sharpe_min = float(gate.get("sharpe_min", 1.0))
    max_dd_pct_abs = float(gate.get("max_dd_pct", 25.0))
    n_min = int(gate.get("n_min", 30))

    # Recompute pass/fail under spec gate (may override doctrine defaults).
    for r in combo_results:
        rep = r["oos_report"]
        passed = (
            rep.returns.sharpe_annualised > sharpe_min
            and abs(rep.returns.max_drawdown_pct) < max_dd_pct_abs
            and rep.trades.n_trades >= n_min
        )
        r["gate_passed_under_spec"] = passed

    # Rank: gate-passers first, then by Sharpe.
    def _rank_key(r):
        rep = r["oos_report"]
        return (not r["gate_passed_under_spec"], -rep.returns.sharpe_annualised)

    ranked = sorted(combo_results, key=_rank_key)

    md = _format_report_text(spec, ranked, sharpe_min, max_dd_pct_abs, n_min)
    return {
        "markdown": md,
        "combos": ranked,
        "spec_path": str(spec_path),
        "kind": kind_name,
    }


def _format_report_text(
    spec: dict,
    ranked: list[dict],
    sharpe_min: float,
    max_dd_pct_abs: float,
    n_min: int,
) -> str:
    meta = spec.get("meta", {})
    lines: list[str] = [
        f"# Quant-strategist run — {meta.get('name', spec.get('kind', '?'))}",
        "",
        f"- Kind: **{spec['kind']}**",
        f"- Source: {meta.get('source', '_no source cited_')}",
        f"- Universe size: {len(spec['universe']['tickers'])} tickers + {spec['universe']['benchmark']} benchmark",
        f"- Period: {spec['period']['start']} → {spec['period']['end']}",
        f"- Walk-forward: {spec.get('walk_forward', {}).get('mode', 'single')}",
        f"- Deployment gate (spec): Sharpe > {sharpe_min} AND |DD| < {max_dd_pct_abs}% AND n ≥ {n_min}",
        f"- Param combinations evaluated: **{len(ranked)}**",
        "",
        "## Ranked combos",
        "",
        "| Rank | Gate | OOS Sharpe | OOS DD | OOS n | OOS CAGR | Params (varied only) |",
        "|---|---|---|---|---|---|---|",
    ]
    # Identify which params varied across combos for compact display.
    varying = _identify_varying_params([r["params"] for r in ranked])
    for i, r in enumerate(ranked, 1):
        rep = r["oos_report"]
        gate = "✅" if r["gate_passed_under_spec"] else "❌"
        varied_str = ", ".join(f"{k}={r['params'][k]}" for k in varying)
        lines.append(
            f"| {i} | {gate} | {rep.returns.sharpe_annualised:.2f} | "
            f"{rep.returns.max_drawdown_pct:+.2f}% | {rep.trades.n_trades} | "
            f"{rep.returns.cagr_pct:+.2f}% | {varied_str or '_(all fixed)_'} |"
        )
    lines.append("")

    # Top combo detail.
    if ranked:
        top = ranked[0]
        lines.extend([
            "## Top combo — full detail",
            "",
            "Params:",
            *(f"- `{k}`: {v}" for k, v in top["params"].items()),
            "",
            _format_report(top["oos_report"], "OOS — DEPLOYMENT GATE"),
        ])
        if "windows" in top:
            lines.extend(["## Top combo — per-window OOS breakdown", "",
                          "| IS window | OOS window | OOS n | OOS Sharpe | OOS DD | Gate |",
                          "|---|---|---|---|---|---|"])
            for w in top["windows"]:
                s = w["spec"]
                gate = "✅" if w["oos_gate"] else "❌"
                lines.append(
                    f"| {s['in_sample_start']}..{s['in_sample_end']} | "
                    f"{s['in_sample_end']}..{s['out_of_sample_end']} | "
                    f"{w['oos_n']} | {w['oos_sharpe']:.2f} | "
                    f"{w['oos_max_dd_pct']:+.2f}% | {gate} |"
                )
            lines.append("")
    lines.extend([
        "## Doctrine verdict",
        "",
        "Per `swing-risk-compliance-doctrine` + `walk-forward-analysis`: a strategy ships to paper-trade (then live) only when **OOS Sharpe > spec.gate.sharpe_min AND |OOS DD| < spec.gate.max_dd_pct AND OOS n ≥ spec.gate.n_min**. Gate-passing combos are deployable candidates; gate-failing combos are research material.",
    ])
    return "\n".join(lines)


def _identify_varying_params(param_dicts: list[dict]) -> list[str]:
    if not param_dicts:
        return []
    varying = []
    keys = list(param_dicts[0].keys())
    for k in keys:
        values = {d.get(k) for d in param_dicts}
        if len(values) > 1:
            varying.append(k)
    return varying


def main() -> None:
    p = argparse.ArgumentParser(prog="tools.quant_strategies.runner")
    p.add_argument("--spec", required=True, help="Path to strategy YAML spec")
    p.add_argument("--out", default=None, help="Write Markdown report to this path (default: stdout only)")
    p.add_argument("--force-refetch", action="store_true")
    args = p.parse_args()

    result = run_spec(args.spec, force_refetch=args.force_refetch)
    print(result["markdown"])
    if args.out:
        Path(args.out).write_text(result["markdown"], encoding="utf-8")
        print(f"\n# Report written to {args.out}", flush=True)


if __name__ == "__main__":
    main()
