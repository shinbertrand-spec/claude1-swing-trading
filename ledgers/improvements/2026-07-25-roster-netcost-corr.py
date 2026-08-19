"""Roster-replenishment sprint (2026-07-25): net-of-cost gate on the
two-clause-PROMOTED combo for each candidate, plus OOS-window return-stream
correlation to the live survivor ts_momentum_liquid_us.

Read-only. Net-of-cost via tools.backtest.portfolio_simulator.simulate
(full-spread marketable + OHLC fills — the hardened gate net_gate_rerun uses).
Correlation: each strategy's FULL-period daily portfolio returns, sliced to the
candidate's OOS window (last-30% split), Pearson r vs ts_momentum on the
intersection of trading dates.
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

# setup -> promoted combo (two-clause top-ranked varied params). {} = single/fixed.
JOBS = {
    "connors_rsi2": {"entry_threshold": 15, "cooldown_days": 5},
    "xs_low_volatility": {"bottom_n": 20},
    "event_insider_buying": {},
    "ts_momentum_liquid_us": {"lookback_days": 252, "top_k": 8},  # deployed survivor
}


def run(setup: str, promoted: dict) -> dict:
    spec = yaml.safe_load(open(f"tools/quant_strategies/{setup}.yml", encoding="utf-8"))
    kind_mod = KIND_REGISTRY[spec["kind"]]
    params = quant_scanner._live_params_for({"deployable_params": promoted}, spec)
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
    print(f"{setup}: net FULL S={full.sharpe_annualised:.2f} MDD={full.max_drawdown_pct:.1f} "
          f"n={full.n_trades} pass={full.deployment_gate_passed} | "
          f"net OOS S={oos.sharpe_annualised:.2f} MDD={oos.max_drawdown_pct:.1f} "
          f"n={oos.n_trades} pass={oos.deployment_gate_passed}", flush=True)
    daily = full.equity_curve.pct_change().dropna()
    daily.index = pd.to_datetime(daily.index)
    return {
        "setup": setup, "gate": (smin, ddmax, nmin), "oos_start": oos_start,
        "full": full, "oos": oos, "daily_full": daily,
    }


def main() -> None:
    res = {s: run(s, p) for s, p in JOBS.items()}
    ts = res["ts_momentum_liquid_us"]["daily_full"]

    lines = ["# Roster sprint — net-of-cost gate (promoted combo) + OOS corr to ts_momentum", ""]
    lines += ["## Net-of-cost gate (hardened portfolio sim, promoted combo)", "",
              "| setup | combo | net FULL Sharpe | full MDD% | full n | net OOS Sharpe | OOS MDD% | OOS n | gate(S/DD/n) | net verdict |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for s, r in res.items():
        f, o = r["full"], r["oos"]
        smin, ddmax, nmin = r["gate"]
        keep = f.deployment_gate_passed and o.deployment_gate_passed
        combo = JOBS[s] or "(fixed)"
        lines.append(
            f"| {s} | {combo} | {f.sharpe_annualised:.2f} | {f.max_drawdown_pct:.1f} | "
            f"{f.n_trades} | {o.sharpe_annualised:.2f} | {o.max_drawdown_pct:.1f} | {o.n_trades} | "
            f"{smin:.1f}/{ddmax:.0f}/{nmin} | {'KEEP' if keep else 'RETIRE'} |")

    lines += ["", "## OOS-window return-stream correlation to ts_momentum_liquid_us", "",
              "Each candidate's FULL-period daily portfolio returns, sliced to its own OOS "
              "window (last-30% split), Pearson r vs ts_momentum over the intersection of "
              "trading dates.", "",
              "| candidate | OOS start | overlap days | Pearson r vs ts_momentum |",
              "|---|---|---|---|"]
    for s in ("connors_rsi2", "xs_low_volatility", "event_insider_buying"):
        r = res[s]
        cand = r["daily_full"]
        oos_start = pd.Timestamp(r["oos_start"])
        cand_oos = cand[cand.index >= oos_start]
        joined = pd.concat([cand_oos.rename("c"), ts.rename("t")], axis=1, join="inner").dropna()
        if len(joined) >= 3 and joined["c"].std() > 0 and joined["t"].std() > 0:
            corr = float(np.corrcoef(joined["c"], joined["t"])[0, 1])
            corr_s = f"{corr:+.3f}"
        else:
            corr_s = f"n/a (n={len(joined)})"
        lines.append(f"| {s} | {oos_start.date()} | {len(joined)} | {corr_s} |")

    out = "ledgers/improvements/2026-07-25-roster-netcost-corr.md"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nwrote {out}", flush=True)


if __name__ == "__main__":
    main()
