"""Companion lookback sweep for the ts_momentum_liquid_us re-validation
(Claude1 paper-research pilot, 2026-07-02). SUGGEST-ONLY artifact.

Mirrors scripts/momentum_fill_recert.py's LIVE realistic-fill harness EXACTLY
(marketable limit prior_close x1.03, FULL effective spread, OHLC fills, 8-cap,
per-window rolling walk-forward), but sweeps the spec's existing
`lookback_days` grid {126, 252} instead of pinning the deployed 252. Purpose:
test whether the shorter, in-grid lookback gives a MORE ROBUST per-window OOS
profile than the deployed 252 — i.e. is there a small, already-specified param
change that de-fragilises this marginal live edge?

Reads ONLY: the same simulator / kind / universe the live scanner uses. Writes
ONLY its own report + JSON under ledgers/improvements/. Does NOT touch
deployable_setups.yml, the spec YAML, or any strategy code / order path.

Usage:  uv run python ledgers/improvements/2026-07-02-ts_momentum-lookback-sweep.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import yaml

from tools.auto_paper import config, quant_scanner
from tools.backtest import portfolio_simulator as psim
from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

SETUP = "ts_momentum_liquid_us"
GRID = [126, 252]           # the spec's existing lookback_days grid
LIVE_BUFFER = 0.03          # matches momentum_fill_recert.LIVE_BUFFER
OUT_MD = os.path.join(_ROOT, "ledgers", "improvements",
                      "2026-07-02-ts_momentum-lookback-sweep.md")
OUT_JSON = os.path.join(_ROOT, "ledgers", "improvements",
                        "2026-07-02-ts_momentum-lookback-sweep.json")


def evaluate(lookback: int, spec: dict, base_params: dict, dfs, benchmark,
             start: date, end: date, gate: dict) -> dict:
    kind_mod = KIND_REGISTRY[spec["kind"]]
    params = dict(base_params)
    params["lookback_days"] = lookback      # the ONLY override vs the deployed config

    state = kind_mod.precompute(dfs, params) if hasattr(kind_mod, "precompute") else None
    signals = []
    for t, df in dfs.items():
        if t == benchmark:
            continue
        signals.extend(kind_mod.replay(df, t, params, state))

    smin = float(gate.get("sharpe_min", 1.0))
    ddmax = float(gate.get("max_dd_pct", 25.0))
    nmin = int(gate.get("n_min", 30))
    mws = float(gate.get("min_window_sharpe", 0.5))
    mwpr = float(gate.get("min_window_pass_rate", 0.5))
    wf = spec.get("walk_forward", {})
    is_y = int(wf.get("is_years", 3)); oos_y = int(wf.get("oos_years", 1)); step_y = int(wf.get("step_years", 1))

    LIVE = dict(fill_model=psim.FILL_MARKETABLE_LIMIT, momentum_buffer=LIVE_BUFFER,
                full_spread_marketable=True)

    full = psim.simulate(signals, dfs, sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin, **LIVE)
    wf_live = psim.simulate_walk_forward(
        signals, dfs, start=start, end=end, is_years=is_y, oos_years=oos_y,
        step_years=step_y, sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin,
        min_window_sharpe=mws, min_window_pass_rate=mwpr, **LIVE)

    full_gate = (full.sharpe_annualised > smin and abs(full.max_drawdown_pct) < ddmax
                 and full.n_trades >= nmin)
    a = wf_live.aggregate
    deploy = bool(full_gate and wf_live.aggregate_gate_passed and wf_live.window_clause_passed)

    passing = [r.sharpe_annualised for _, r in wf_live.window_results
               if r.sharpe_annualised > mws]
    weakest_pass = min(passing) if passing else 0.0
    rate_margin = wf_live.window_pass_rate - mwpr
    fragile = deploy and (rate_margin <= 1e-9 or (weakest_pass - mws) <= 0.05)
    if deploy and not fragile:
        verdict = "DEPLOY"
    elif deploy:
        verdict = "DEPLOY — MARGINAL/FRAGILE"
    else:
        verdict = "RETIRE / REBUILD"

    windows = [{"is_end_year": sp.in_sample_end.year,
                "oos_sharpe": r.sharpe_annualised,
                "oos_n": r.n_trades,
                "clears": bool(r.sharpe_annualised > mws)}
               for sp, r in wf_live.window_results]

    return {
        "lookback_days": lookback,
        "n_signals": len(signals),
        "full": {"sharpe": full.sharpe_annualised, "mdd": full.max_drawdown_pct,
                 "n": full.n_trades, "fill": full.fill_rate, "gate": full_gate},
        "oos_agg": {"sharpe": a.sharpe_annualised, "mdd": a.max_drawdown_pct,
                    "n": a.n_trades, "fill": a.fill_rate,
                    "gate": bool(wf_live.aggregate_gate_passed)},
        "per_window": windows,
        "n_windows": wf_live.n_windows,
        "n_windows_above_floor": wf_live.n_windows_above_floor,
        "window_pass_rate": wf_live.window_pass_rate,
        "window_clause_passed": bool(wf_live.window_clause_passed),
        "weakest_passing_window": weakest_pass,
        "verdict": verdict,
    }


def main() -> None:
    data = config.load()
    rows = {r["setup"]: r for r in data.get("deployable", [])
            if isinstance(r, dict) and "setup" in r}
    row = rows.get(SETUP, {})
    spec = yaml.safe_load(open(os.path.join(_ROOT, "tools", "quant_strategies",
                                            f"{SETUP}.yml"), encoding="utf-8"))
    base_params = quant_scanner._live_params_for(row or {}, spec)
    benchmark = base_params.get("benchmark") or spec["universe"]["benchmark"]
    gate = spec.get("gate", {})

    tickers = resolve_universe_tickers(spec)
    if benchmark not in tickers:
        tickers.append(benchmark)
    start = date.fromisoformat(str(spec["period"]["start"]))
    end = date.fromisoformat(str(spec["period"]["end"]))

    print(f"loading {SETUP} universe ({len(tickers)} tickers) ...", flush=True)
    dfs = _load_universe(tickers, start, end, force_refetch=False)

    results = []
    for lb in GRID:
        r = evaluate(lb, spec, base_params, dfs, benchmark, start, end, gate)
        results.append(r)
        wins = " ".join(f"{w['is_end_year']}:{w['oos_sharpe']:.2f}"
                        f"{'v' if w['clears'] else 'x'}" for w in r["per_window"])
        deployed = "  <- deployed" if lb == 252 else ""
        print(f"lookback={lb}{deployed}: FULL S={r['full']['sharpe']:.2f} "
              f"MDD={r['full']['mdd']:.1f} n={r['full']['n']} | "
              f"OOS-agg S={r['oos_agg']['sharpe']:.2f} | "
              f"per-window {r['n_windows_above_floor']}/{r['n_windows']} [{wins}] "
              f"-> {r['verdict']}", flush=True)

    lines = [
        f"# {SETUP} — lookback_days sweep through the realistic fill-recert harness (pilot, 2026-07-02)",
        "",
        "Same LIVE execution model as scripts/momentum_fill_recert.py "
        "(marketable limit prior_close x1.03, FULL effective spread, OHLC fills, "
        "8-concurrent cap, rolling 3y-IS/1y-OOS/1y-step walk-forward). "
        "Gate: Sharpe>1.0 & |MDD|<25% & n>=30 on FULL & OOS-aggregate, "
        "AND >=50% of OOS windows clear Sharpe>0.5. Only lookback_days varies.",
        "Deployed config is lookback_days=252.",
        "",
        "| lookback | n_sig | FULL S | FULL MDD% | FULL n | fill% | OOS-agg S | "
        "per-window (clears/total) | weakest pass | VERDICT |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        f = r["full"]; o = r["oos_agg"]
        deployed = "  ← deployed" if r["lookback_days"] == 252 else ""
        wins = " ".join(f"{w['is_end_year']}:{w['oos_sharpe']:.2f}"
                        f"{'✓' if w['clears'] else '✗'}" for w in r["per_window"])
        lines.append(
            f"| {r['lookback_days']}{deployed} | {r['n_signals']} | {f['sharpe']:.2f} | "
            f"{abs(f['mdd']):.1f} | {f['n']} | {f['fill']*100:.0f} | {o['sharpe']:.2f} | "
            f"{r['n_windows_above_floor']}/{r['n_windows']} ({wins}) | "
            f"{r['weakest_passing_window']:.2f} | **{r['verdict']}** |"
        )
    lines.append("")
    lines.append("Interpretation: a lookback whose per-window clears/total is HIGHER "
                 "(and whose weakest passing window sits further above the 0.5 floor) "
                 "is the more robust config. If neither beats 3/6-at-the-floor, the "
                 "fragility is a property of the ts_momentum signal on this universe, "
                 "not of the lookback choice, and the deployed 252 stands as-is.")

    with open(OUT_MD, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"\nwrote {OUT_MD}\nwrote {OUT_JSON}", flush=True)


if __name__ == "__main__":
    main()
