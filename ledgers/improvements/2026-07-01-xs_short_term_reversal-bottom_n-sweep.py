"""Companion net-of-cost sweep for the xs_short_term_reversal re-validation
(Claude1 paper-research pilot, 2026-07-01). SUGGEST-ONLY artifact.

Mirrors scripts/net_gate_rerun.evaluate_setup EXACTLY (same hardened
net-of-cost portfolio simulator, same OOS last-30% split, same spec gate),
but sweeps the bottom_n grid {5, 10, 15} instead of pinning the deployed
bottom_n=5. Purpose: show the RETIRE verdict is robust across the parameter
neighbourhood, i.e. no small param change inside the existing grid rescues
the edge net-of-cost.

Read-only: imports the repo's live simulator/kind/universe; writes ONLY its
own report + JSON under ledgers/improvements/. Does NOT touch
deployable_setups.yml or any strategy code.

Usage:  uv run python ledgers/improvements/2026-07-01-xs_short_term_reversal-bottom_n-sweep.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import yaml

from tools.auto_paper import config, quant_scanner
from tools.backtest import portfolio_simulator as psim
from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

SETUP = "xs_short_term_reversal"
GRID = [5, 10, 15]
OUT_MD = os.path.join(_ROOT, "ledgers", "improvements",
                      "2026-07-01-xs_short_term_reversal-bottom_n-sweep.md")
OUT_JSON = os.path.join(_ROOT, "ledgers", "improvements",
                        "2026-07-01-xs_short_term_reversal-bottom_n-sweep.json")


def evaluate(bottom_n: int, spec: dict, row: dict) -> dict:
    kind_mod = KIND_REGISTRY[spec["kind"]]
    params = quant_scanner._live_params_for(row or {}, spec)
    params["bottom_n"] = bottom_n  # the ONLY override vs the deployed config
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

    keep = full.deployment_gate_passed and oos.deployment_gate_passed
    return {
        "bottom_n": bottom_n,
        "n_signals": len(signals),
        "gate": {"sharpe_min": smin, "max_dd_pct": ddmax, "n_min": nmin},
        "full": {"sharpe": full.sharpe_annualised, "mdd": full.max_drawdown_pct,
                 "n": full.n_trades, "fill": full.fill_rate, "ret": tot_ret(full),
                 "filled_fwd": full.avg_filled_fwd_return,
                 "missed_fwd": full.avg_missed_fwd_return,
                 "pass": full.deployment_gate_passed},
        "oos": {"sharpe": oos.sharpe_annualised, "mdd": oos.max_drawdown_pct,
                "n": oos.n_trades, "fill": oos.fill_rate, "ret": tot_ret(oos),
                "pass": oos.deployment_gate_passed},
        "verdict": "KEEP" if keep else "RETIRE",
    }


def main() -> None:
    data = config.load()
    rows = {r["setup"]: r for r in data.get("deployable", [])
            if isinstance(r, dict) and "setup" in r}
    spec = yaml.safe_load(open(os.path.join(_ROOT, "tools", "quant_strategies",
                                            f"{SETUP}.yml"), encoding="utf-8"))
    row = rows.get(SETUP, {})

    results = []
    for bn in GRID:
        r = evaluate(bn, spec, row)
        results.append(r)
        print(f"bottom_n={bn}: FULL S={r['full']['sharpe']:.2f} "
              f"MDD={r['full']['mdd']:.1f} n={r['full']['n']} "
              f"fill={r['full']['fill']*100:.0f}% | OOS S={r['oos']['sharpe']:.2f} "
              f"MDD={r['oos']['mdd']:.1f} n={r['oos']['n']} -> {r['verdict']}",
              flush=True)

    lines = [
        f"# {SETUP} — bottom_n sweep through the net-of-cost gate (pilot, 2026-07-01)",
        "",
        "Same hardened net-of-cost simulator as scripts/net_gate_rerun.py "
        "(cost_model + OHLC fills + cap-weight), same OOS last-30% split, "
        "same spec gate (Sharpe>1.0 / |MDD|<25% / n>=30). Only bottom_n varies.",
        "Deployed config is bottom_n=5 (first combo of the grid).",
        "",
        "| bottom_n | n_sig | FULL net Sharpe | full MDD% | full n | fill% | full ret% "
        "| filled_fwd% | missed_fwd% | OOS Sharpe | OOS MDD% | OOS n | VERDICT |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        f, o = r["full"], r["oos"]
        deployed = "  ← deployed" if r["bottom_n"] == 5 else ""
        lines.append(
            f"| {r['bottom_n']}{deployed} | {r['n_signals']} | {f['sharpe']:.2f} | "
            f"{f['mdd']:.1f} | {f['n']} | {f['fill']*100:.0f} | {f['ret']*100:.1f} | "
            f"{f['filled_fwd']*100:.2f} | {f['missed_fwd']*100:.2f} | "
            f"{o['sharpe']:.2f} | {o['mdd']:.1f} | {o['n']} | **{r['verdict']}** |"
        )
    lines.append("")
    lines.append("Interpretation: if every row RETIREs, the net-of-cost failure is a "
                 "property of the signal (adverse selection: limit orders fill the "
                 "losers that keep falling, miss the winners that gap up), not of the "
                 "bottom_n choice.")

    with open(OUT_MD, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"\nwrote {OUT_MD}\nwrote {OUT_JSON}", flush=True)


if __name__ == "__main__":
    main()
