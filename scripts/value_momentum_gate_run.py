"""Run the integrated value+momentum KIND through the hardened net-of-cost gate,
ALONGSIDE its value-only and momentum-only baselines (derived from the same spec
by overriding value_weight) — on identical universe / period / banding / costs.

Success bar (all required to PROMOTE to paper):
  1. Hardened gate: net Sharpe>1.0 AND |MDD|<25% AND n>=30, on BOTH full + OOS.
  2. In-sample per-trade t > 3 (stricter than the usual t>2 — skepticism stack).
  3. Integrated beats BOTH the value-only AND momentum-only baselines (net OOS
     Sharpe), by a margin (>=0.10) — else "just run the better standalone";
     integration must earn its complexity.
Also reported (honest, not assumed): the realized value/momentum return
correlation on THIS data, and a ~50% decay haircut as the live Sharpe expectation.

Fails any clause → RETIRE (or run the standalone that did clear the gate). Same
discipline that retired the insider KIND + 4/5 generic setups. Do NOT tune.

Usage:  uv run python scripts/value_momentum_gate_run.py [--spec PATH] [--out PATH]
        [--no-facts]   # momentum-only smoke (skips the SEC fundamentals fetch)
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import yaml

from tools.backtest import portfolio_simulator as psim
from tools.fundamentals import pit_fundamentals as pf
from tools.quant_strategies._kinds import value_momentum_integrated as vmi
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

DEFAULT_SPEC = "tools/quant_strategies/value_momentum_integrated.yml"
HAIRCUT = 0.5            # decay haircut applied to OOS Sharpe -> live expectation
MARGIN = 0.10           # integrated must beat best standalone by this Sharpe


def _tot_ret(res) -> float:
    ec = res.equity_curve
    return (float(ec.iloc[-1]) / float(ec.iloc[0]) - 1.0) if len(ec) else 0.0


def _build_facts(tickers: list[str]) -> dict:
    cik_map = pf.load_ticker_cik_map()
    out, miss = {}, 0
    for i, t in enumerate(tickers):
        cik = cik_map.get(t)
        if not cik:
            miss += 1
            continue
        try:
            out[t] = pf.fetch_company_facts(cik)
        except Exception:        # noqa: BLE001
            miss += 1
        if (i + 1) % 200 == 0:
            print(f"  facts {i+1}/{len(tickers)} (have {len(out)}, missing {miss})", flush=True)
    print(f"  facts done: {len(out)}/{len(tickers)} have company-facts", flush=True)
    return out


def _is_tstat(trades, oos_start: date) -> tuple[float, int]:
    """Per-trade net-return t-stat on the IN-SAMPLE window (fill_date < oos_start)."""
    rs = [t.net_return for t in trades if t.fill_date < oos_start]
    n = len(rs)
    if n < 2:
        return 0.0, n
    mu = sum(rs) / n
    sd = math.sqrt(sum((r - mu) ** 2 for r in rs) / (n - 1))
    if sd == 0:
        return 0.0, n
    return mu / (sd / math.sqrt(n)), n


def _daily_returns(res):
    ec = res.equity_curve
    return ec.pct_change().dropna() if len(ec) else None


def run_variant(name, value_w, dfs, params_base, facts, start, end, gate, oos_start):
    params = dict(params_base)
    params["value_weight"] = value_w
    params["momentum_weight"] = 1.0 - value_w
    if value_w != 0:
        params["_facts_by_ticker"] = facts
    benchmark = params.get("benchmark")
    state = vmi.precompute(dfs, params)
    signals = []
    for t, df in dfs.items():
        if t == benchmark:
            continue
        signals.extend(vmi.replay(df, t, params, state))
    smin, ddmax, nmin = gate
    oos_sigs = [s for s in signals if s.fill_date >= oos_start]
    full = psim.simulate(signals, dfs, sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin)
    oos = psim.simulate(oos_sigs, dfs, sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin)
    tstat, n_is = _is_tstat(full.trades, oos_start)
    n_concurrent_holders = len(state.spans_by_ticker)
    return {
        "name": name, "value_w": value_w, "n_signals": len(signals),
        "n_names": n_concurrent_holders, "tstat_is": tstat, "n_is": n_is,
        "full": full, "oos": oos,
        "full_row": (full.sharpe_annualised, full.max_drawdown_pct, full.n_trades,
                     full.fill_rate, _tot_ret(full), full.deployment_gate_passed),
        "oos_row": (oos.sharpe_annualised, oos.max_drawdown_pct, oos.n_trades,
                    oos.fill_rate, _tot_ret(oos), oos.deployment_gate_passed),
    }


def run(spec_path: str, use_facts: bool = True) -> dict:
    spec = yaml.safe_load(open(spec_path, encoding="utf-8"))
    params_base = dict(spec.get("params", {}))
    benchmark = spec["universe"]["benchmark"]
    params_base.setdefault("benchmark", benchmark)
    tickers = resolve_universe_tickers(spec)
    if benchmark not in tickers:
        tickers.append(benchmark)
    start = date.fromisoformat(str(spec["period"]["start"]))
    end = date.fromisoformat(str(spec["period"]["end"]))
    g = spec.get("gate", {})
    gate = (float(g.get("sharpe_min", 1.0)), float(g.get("max_dd_pct", 25.0)),
            int(g.get("n_min", 30)))
    oos_start = start + timedelta(days=int((end - start).days * 0.70))

    print(f"loading {len(tickers)} tickers {start}..{end} ...", flush=True)
    dfs = _load_universe(tickers, start, end, force_refetch=False)
    print(f"loaded {len(dfs)} price frames", flush=True)

    facts = {}
    if use_facts:
        print("fetching SEC company-facts (cached) ...", flush=True)
        facts = _build_facts([t for t in tickers if t != benchmark])

    variants = [("momentum_only", 0.0)]
    if use_facts:
        variants = [("integrated", float(params_base.get("value_weight", 0.5))),
                    ("value_only", 1.0), ("momentum_only", 0.0)]

    results = {}
    for name, vw in variants:
        print(f"running {name} (value_weight={vw}) ...", flush=True)
        results[name] = run_variant(name, vw, dfs, params_base, facts,
                                    start, end, gate, oos_start)

    # realized value/momentum correlation on THIS data (portfolio daily returns)
    corr = None
    if "value_only" in results and "momentum_only" in results:
        rv = _daily_returns(results["value_only"]["oos"])
        rm = _daily_returns(results["momentum_only"]["oos"])
        if rv is not None and rm is not None:
            j = rv.index.intersection(rm.index)
            if len(j) > 5:
                corr = float(np.corrcoef(rv.loc[j], rm.loc[j])[0, 1])

    return {"spec": spec_path, "gate": gate, "oos_start": oos_start,
            "results": results, "vm_corr": corr, "use_facts": use_facts}


def _verdict(r: dict) -> tuple[str, list[str]]:
    res = r["results"]
    if "integrated" not in res:
        return "N/A (momentum-only smoke)", ["ran without fundamentals (--no-facts)"]
    integ = res["integrated"]
    notes = []
    gate_pass = integ["full_row"][5] and integ["oos_row"][5]
    notes.append(f"gate (full+OOS net Sharpe>1, |MDD|<25, n>=30): {'PASS' if gate_pass else 'FAIL'}")
    t_pass = integ["tstat_is"] > 3.0
    notes.append(f"in-sample per-trade t={integ['tstat_is']:.2f} (need >3): {'PASS' if t_pass else 'FAIL'}")
    io = integ["oos_row"][0]
    vo = res["value_only"]["oos_row"][0]
    mo = res["momentum_only"]["oos_row"][0]
    best_std = max(vo, mo)
    beats = io >= best_std + MARGIN
    notes.append(f"beats baselines by >={MARGIN}: integrated OOS Sharpe {io:.2f} vs "
                 f"value {vo:.2f} / momentum {mo:.2f} (best {best_std:.2f}): "
                 f"{'PASS' if beats else 'FAIL'}")
    n_oos = integ["oos_row"][2]
    if n_oos < 30:
        notes.append(f"CAVEAT: integrated OOS n={n_oos} (<30) -- the baseline-beat is "
                     f"NOT statistically load-bearing; banding + 8-cap starve the "
                     f"factor of breadth/trade-count.")
    if gate_pass and t_pass and beats:
        return "PROMOTE (paper)", notes
    # if a standalone cleared the gate but integrated didn't earn complexity:
    for sname in ("value_only", "momentum_only"):
        s = res[sname]
        if s["full_row"][5] and s["oos_row"][5] and not (gate_pass and beats):
            notes.append(f"NOTE: {sname} cleared the gate — prefer the standalone "
                         f"over the integrated book.")
    return "RETIRE", notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default=DEFAULT_SPEC)
    ap.add_argument("--out", default="journal/backtest/value-momentum-gate.md")
    ap.add_argument("--no-facts", action="store_true",
                    help="momentum-only smoke (skip the SEC fundamentals fetch)")
    args = ap.parse_args()

    r = run(args.spec, use_facts=not args.no_facts)
    verdict, notes = _verdict(r)
    smin, ddmax, nmin = r["gate"]

    corr_line = (f"- Realized value<->momentum daily-return correlation (OOS): {r['vm_corr']:.2f}"
                 if r["vm_corr"] is not None
                 else "- Realized value<->momentum correlation: n/a")
    lines = ["# Integrated Value+Momentum -- net-of-cost gate + baselines", "",
             f"- Spec: `{r['spec']}`  --  gate: Sharpe>{smin} AND |MDD|<{ddmax}% AND n>={nmin}, net, on full+OOS",
             f"- OOS split: fills on/after {r['oos_start']} (last 30%)",
             corr_line, ""]
    lines += ["| variant | window | net Sharpe | MDD% | n | fill% | ret% | pass |",
              "|---|---|---|---|---|---|---|---|"]
    for name, v in r["results"].items():
        for win, row in (("full", v["full_row"]), ("OOS", v["oos_row"])):
            lines.append(f"| {name} | {win} | {row[0]:.2f} | {row[1]:.1f} | {row[2]} | "
                         f"{row[3]*100:.0f} | {row[4]*100:.1f} | {row[5]} |")
    lines += ["", "## Skepticism checks", ""]
    for n in notes:
        lines.append(f"- {n}")
    if "integrated" in r["results"]:
        io = r["results"]["integrated"]["oos_row"][0]
        lines.append(f"- ~{int(HAIRCUT*100)}% decay haircut -> live Sharpe expectation "
                     f"~ {io*HAIRCUT:.2f} (OOS {io:.2f} x {HAIRCUT})")
    lines += ["", f"## VERDICT: **{verdict}**", ""]

    out = args.out
    if not os.path.isabs(out):
        out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print("\n".join(lines[-12:]), flush=True)
    print(f"wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
