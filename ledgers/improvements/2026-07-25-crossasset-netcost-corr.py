"""Cross-asset trend (Phase 0) — net-of-cost gate + OOS correlation to the live
survivor ts_momentum_liquid_us. Reads the inline-universe spec from
ledgers/improvements/ (no tools/ write). Read-only.

Runs both declared lookbacks (126, 252). Net-of-cost via
portfolio_simulator.simulate (hardened model). Correlation: cross-asset book's
FULL-period daily returns sliced to its OOS window (last-30% split), Pearson r
vs ts_momentum over the intersection of trading dates.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import date, timedelta

import numpy as np
import pandas as pd
import yaml

from tools.auto_paper import quant_scanner
from tools.backtest import portfolio_simulator as psim
from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

CROSSASSET_SPEC = "ledgers/improvements/2026-07-25-crossasset_trend.yml"


def _collect_signals(spec: dict, params: dict, dfs: dict, benchmark: str):
    kind_mod = KIND_REGISTRY[spec["kind"]]
    state = kind_mod.precompute(dfs, params) if hasattr(kind_mod, "precompute") else None
    signals = []
    for t, df in dfs.items():
        if t == benchmark:
            continue
        signals.extend(kind_mod.replay(df, t, params, state))
    return signals


def run_spec_file(spec_path: str, promoted: dict, label: str) -> dict:
    spec = yaml.safe_load(open(spec_path, encoding="utf-8"))
    benchmark = spec["universe"]["benchmark"]
    tickers = resolve_universe_tickers(spec)
    if benchmark not in tickers:
        tickers.append(benchmark)
    start = date.fromisoformat(str(spec["period"]["start"]))
    end = date.fromisoformat(str(spec["period"]["end"]))
    dfs = _load_universe(tickers, start, end, force_refetch=False)
    # first-combo params + promoted overrides
    base = quant_scanner._live_params_for({"deployable_params": promoted}, spec)
    signals = _collect_signals(spec, base, dfs, benchmark)
    gate = spec.get("gate", {})
    smin = float(gate.get("sharpe_min", 1.0)); ddmax = float(gate.get("max_dd_pct", 25.0)); nmin = int(gate.get("n_min", 30))
    oos_start = start + timedelta(days=int((end - start).days * 0.70))
    oos_sigs = [s for s in signals if s.fill_date >= oos_start]
    full = psim.simulate(signals, dfs, sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin)
    oos = psim.simulate(oos_sigs, dfs, sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin)
    print(f"{label}: net FULL S={full.sharpe_annualised:.2f} MDD={full.max_drawdown_pct:.1f} "
          f"n={full.n_trades} pass={full.deployment_gate_passed} | net OOS S={oos.sharpe_annualised:.2f} "
          f"MDD={oos.max_drawdown_pct:.1f} n={oos.n_trades} pass={oos.deployment_gate_passed}", flush=True)
    daily = full.equity_curve.pct_change().dropna()
    daily.index = pd.to_datetime(daily.index)
    return {"label": label, "gate": (smin, ddmax, nmin), "oos_start": oos_start,
            "full": full, "oos": oos, "daily_full": daily}


def main() -> None:
    # ts_momentum_liquid_us baseline (deployed params) for correlation.
    ts = run_spec_file  # alias avoids confusion
    tsr = _run_ts_baseline()
    ts_daily = tsr["daily_full"]

    results = []
    for lb in (126, 252):
        results.append(run_spec_file(CROSSASSET_SPEC, {"lookback_days": lb, "top_k": 8},
                                     f"crossasset_trend lb={lb}"))

    lines = ["# Cross-asset trend (Phase 0) — net-of-cost + OOS corr to ts_momentum", ""]
    lines += ["## Net-of-cost gate (hardened portfolio sim)", "",
              "| variant | net FULL Sharpe | full MDD% | full n | net OOS Sharpe | OOS MDD% | OOS n | net verdict |",
              "|---|---|---|---|---|---|---|---|"]
    for r in [tsr] + results:
        f, o = r["full"], r["oos"]
        keep = f.deployment_gate_passed and o.deployment_gate_passed
        lines.append(f"| {r['label']} | {f.sharpe_annualised:.2f} | {f.max_drawdown_pct:.1f} | {f.n_trades} | "
                     f"{o.sharpe_annualised:.2f} | {o.max_drawdown_pct:.1f} | {o.n_trades} | "
                     f"{'KEEP' if keep else 'RETIRE'} |")

    lines += ["", "## OOS-window return-stream correlation to ts_momentum_liquid_us", "",
              "| variant | OOS start | overlap days | Pearson r vs ts_momentum |",
              "|---|---|---|---|"]
    for r in results:
        cand = r["daily_full"]
        oos_start = pd.Timestamp(r["oos_start"])
        cand_oos = cand[cand.index >= oos_start]
        joined = pd.concat([cand_oos.rename("c"), ts_daily.rename("t")], axis=1, join="inner").dropna()
        if len(joined) >= 3 and joined["c"].std() > 0 and joined["t"].std() > 0:
            corr = float(np.corrcoef(joined["c"], joined["t"])[0, 1])
            corr_s = f"{corr:+.3f}"
        else:
            corr_s = f"n/a (n={len(joined)})"
        lines.append(f"| {r['label']} | {oos_start.date()} | {len(joined)} | {corr_s} |")

    out = "ledgers/improvements/2026-07-25-crossasset-netcost-corr.md"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nwrote {out}", flush=True)


def _run_ts_baseline() -> dict:
    spec = yaml.safe_load(open("tools/quant_strategies/ts_momentum_liquid_us.yml", encoding="utf-8"))
    benchmark = spec["universe"]["benchmark"]
    tickers = resolve_universe_tickers(spec)
    if benchmark not in tickers:
        tickers.append(benchmark)
    start = date.fromisoformat(str(spec["period"]["start"]))
    end = date.fromisoformat(str(spec["period"]["end"]))
    dfs = _load_universe(tickers, start, end, force_refetch=False)
    params = quant_scanner._live_params_for({"deployable_params": {"lookback_days": 252, "top_k": 8}}, spec)
    signals = _collect_signals(spec, params, dfs, benchmark)
    gate = spec.get("gate", {})
    smin = float(gate.get("sharpe_min", 1.0)); ddmax = float(gate.get("max_dd_pct", 25.0)); nmin = int(gate.get("n_min", 30))
    oos_start = start + timedelta(days=int((end - start).days * 0.70))
    oos_sigs = [s for s in signals if s.fill_date >= oos_start]
    full = psim.simulate(signals, dfs, sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin)
    oos = psim.simulate(oos_sigs, dfs, sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin)
    print(f"ts_momentum_liquid_us (baseline): net FULL S={full.sharpe_annualised:.2f} "
          f"OOS S={oos.sharpe_annualised:.2f}", flush=True)
    daily = full.equity_curve.pct_change().dropna()
    daily.index = pd.to_datetime(daily.index)
    return {"label": "ts_momentum_liquid_us (baseline)", "oos_start": oos_start,
            "full": full, "oos": oos, "daily_full": daily}


if __name__ == "__main__":
    main()
