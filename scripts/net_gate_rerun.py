"""Net-of-cost gate re-run for the live generic deployables (Phase 1e/1f, 2026-06-17).

Collects each live setup's signals with its DEPLOYED params (matching the live
scanner via quant_scanner._live_params_for), then judges them through the
hardened portfolio-equity simulator (net-of-cost, OHLC fills, cap-weight) over
both the full period and an OOS (last 30%) split. Prints a verdict table.

Read-only: does NOT edit deployable_setups.yml. The operator retires failures
after reviewing the numbers.

Usage:  uv run python scripts/net_gate_rerun.py [--out PATH] [--setup NAME]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from tools.auto_paper import config, quant_scanner
from tools.backtest import portfolio_simulator as psim
from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

LIVE_SETUPS = [
    "ts_momentum_liquid_us",
    "residual_momentum_liquid_us",
    "clenow_momentum_liquid_us",
    "xs_short_term_reversal",
    "xs_short_term_reversal_liquid_us",
]


def _spec_path(setup: str) -> str:
    return f"tools/quant_strategies/{setup}.yml"


def evaluate_setup(setup: str, row: dict) -> dict:
    spec = yaml.safe_load(open(_spec_path(setup), encoding="utf-8"))
    kind = spec["kind"]
    kind_mod = KIND_REGISTRY[kind]
    params = quant_scanner._live_params_for(row or {}, spec)
    benchmark = params.get("benchmark") or spec["universe"]["benchmark"]
    tickers = resolve_universe_tickers(spec)
    if benchmark not in tickers:
        tickers.append(benchmark)
    start = date.fromisoformat(str(spec["period"]["start"]))
    end = date.fromisoformat(str(spec["period"]["end"]))

    dfs = _load_universe(tickers, start, end, force_refetch=False)
    state = kind_mod.precompute(dfs, params) if hasattr(kind_mod, "precompute") else None

    signals = []
    for t, df in dfs.items():
        if t == benchmark:
            continue
        signals.extend(kind_mod.replay(df, t, params, state))

    gate = spec.get("gate", {})
    smin = float(gate.get("sharpe_min", 1.0))
    ddmax = float(gate.get("max_dd_pct", 25.0))
    nmin = int(gate.get("n_min", 30))

    oos_start = start + timedelta(days=int((end - start).days * 0.70))
    oos_sigs = [s for s in signals if s.fill_date >= oos_start]

    full = psim.simulate(signals, dfs, sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin)
    oos = psim.simulate(oos_sigs, dfs, sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin)

    def tot_ret(res):
        ec = res.equity_curve
        return (float(ec.iloc[-1]) / float(ec.iloc[0]) - 1.0) if len(ec) else 0.0

    return {
        "setup": setup, "kind": kind, "n_signals": len(signals),
        "gate": (smin, ddmax, nmin),
        "full": {"sharpe": full.sharpe_annualised, "mdd": full.max_drawdown_pct,
                 "n": full.n_trades, "fill": full.fill_rate, "ret": tot_ret(full),
                 "filled_fwd": full.avg_filled_fwd_return, "missed_fwd": full.avg_missed_fwd_return,
                 "pass": full.deployment_gate_passed},
        "oos": {"sharpe": oos.sharpe_annualised, "mdd": oos.max_drawdown_pct,
                "n": oos.n_trades, "fill": oos.fill_rate, "ret": tot_ret(oos),
                "pass": oos.deployment_gate_passed},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="scripts/net_gate_results.md")
    ap.add_argument("--setup", default=None, help="run a single setup")
    args = ap.parse_args()

    data = config.load()
    rows = {r["setup"]: r for r in data.get("deployable", []) if isinstance(r, dict) and "setup" in r}
    setups = [args.setup] if args.setup else LIVE_SETUPS

    lines = ["# Net-of-cost gate re-run (Phase 1f)", "",
             "| setup | n_sig | FULL net Sharpe | full MDD% | full n | fill% | full ret% | OOS Sharpe | OOS MDD% | OOS n | gate(S/DD/n) | VERDICT |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for setup in setups:
        try:
            r = evaluate_setup(setup, rows.get(setup, {}))
        except Exception as exc:
            lines.append(f"| {setup} | ERROR | {exc!r} | | | | | | | | | ERROR |")
            print(f"{setup}: ERROR {exc!r}", flush=True)
            continue
        f, o = r["full"], r["oos"]
        smin, ddmax, nmin = r["gate"]
        # Verdict: KEEP only if BOTH full and OOS clear the spec gate net-of-cost.
        keep = f["pass"] and o["pass"]
        verdict = "KEEP" if keep else "RETIRE"
        lines.append(
            f"| {setup} | {r['n_signals']} | {f['sharpe']:.2f} | {f['mdd']:.1f} | {f['n']} | "
            f"{f['fill']*100:.0f} | {f['ret']*100:.1f} | {o['sharpe']:.2f} | {o['mdd']:.1f} | {o['n']} | "
            f"{smin:.1f}/{ddmax:.0f}/{nmin} | **{verdict}** |"
        )
        lines.append(
            f"|   ↳ fwd-return filled vs missed (full): {f['filled_fwd']*100:.2f}% vs "
            f"{f['missed_fwd']*100:.2f}% | | | | | | | | | | | |"
        )
        print(f"{setup}: full S={f['sharpe']:.2f} MDD={f['mdd']:.1f} n={f['n']} "
              f"fill={f['fill']*100:.0f}% ret={f['ret']*100:.1f}% | OOS S={o['sharpe']:.2f} "
              f"-> {verdict}", flush=True)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nwrote {args.out}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
