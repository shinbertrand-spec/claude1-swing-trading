"""event_earnings_drift Stage A — validation-chain steps 2-6 on the grid passers.
Claude1 build session, 2026-08-06. Spec §4 chain; step 1 (run_spec two-clause + DSR)
PASSED for ranks 1-2 — this script runs the rest with the SAME machinery that
retired 4/5 setups in the 2026-06-17 net-cost sweep:

  step 2  net-of-cost re-runs:
          (a) net_gate_rerun replica — psim.simulate FULL + OOS(last-30%) split,
              default cost model, incl. fill rate + filled-vs-missed fwd returns
              (the diagnostic that convicted event_insider_buying);
          (b) net-of-cost WALK-FORWARD with the LIVE fill config
              (FILL_MARKETABLE_LIMIT + 3% momentum buffer + FULL effective
              spread) — the binding standard from the July revalidations.
  step 3  DSR >= 0.95 on the net walk-forward aggregate curve, n_trials=91.
  step 4  correlation vs ts_momentum_liquid_us (deployed params) daily equity
          returns under the same net walk-forward config.
  step 6  fill-rate guard (spec §2): fill < 60% OR missed >> filled -> execution-
          model-mismatch verdict, regardless of Sharpe. Reported for BOTH combos.
  (step 5 CPCV only if 2-4 pass — run separately if reached.)

Usage:  uv run python ledgers/improvements/2026-08-06-earnings-drift-chain-steps2-6.py
"""
from __future__ import annotations
import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from datetime import date, timedelta
import pandas as pd
import yaml

from tools.backtest import portfolio_simulator as psim, sharpe_stats as ss
from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

N_TRIALS = 91          # post-registration registry (ledgers/trials.yml, 2026-08-06)
GATE = dict(sharpe_min=1.0, max_dd_pct=25.0, n_min=30, min_window_sharpe=0.5, min_window_pass_rate=0.5)
LIVE = dict(fill_model=psim.FILL_MARKETABLE_LIMIT, momentum_buffer=0.03, full_spread_marketable=True)
PASSERS = [
    {"label": "rank1 ear20/hist_off/hold21", "ear_top_pct": 20, "history_condition": False, "max_hold_days": 21},
    {"label": "rank2 ear10/hist_on/hold10", "ear_top_pct": 10, "history_condition": True, "max_hold_days": 10},
]

spec = yaml.safe_load(open(os.path.join(_ROOT, "tools/quant_strategies/event_earnings_drift.yml"), encoding="utf-8"))
bench = spec["universe"]["benchmark"]
START = date.fromisoformat(str(spec["period"]["start"]))
END = date.fromisoformat(str(spec["period"]["end"]))
FIXED = {k: v for k, v in spec.get("params", {}).items() if not isinstance(v, list)}
FIXED.setdefault("benchmark", bench)


def build_signals(kind_name, params, dfs):
    km = KIND_REGISTRY[kind_name]
    state = km.precompute(dfs, params)
    out = []
    for t, df in dfs.items():
        if t != bench:
            out.extend(km.replay(df, t, params, state))
    return out


def dsr_of(equity, n_trials=N_TRIALS):
    r = pd.Series(equity).pct_change().dropna()
    if len(r) < 30:
        return float("nan")
    m = ss.sharpe_moments(list(r.values))
    return float(ss.deflated_sharpe(m["sr"], int(m["n"]), m["skew"], m["kurt_raw"],
                                    var_trials=ss.annualized_to_per_period(0.5) ** 2,
                                    n_trials=n_trials)["dsr"])


def main():
    tickers = resolve_universe_tickers(spec)
    if bench not in tickers:
        tickers.append(bench)
    print(f"loading {len(tickers)} tickers ...", flush=True)
    dfs = _load_universe(tickers, START, END, force_refetch=False)

    # ts_momentum reference (step 4) — deployed params per its spec
    ts_spec = yaml.safe_load(open(os.path.join(_ROOT, "tools/quant_strategies/ts_momentum_liquid_us.yml"), encoding="utf-8"))
    ts_params = {k: (v if not isinstance(v, list) else v[-1]) for k, v in ts_spec.get("params", {}).items()}
    ts_params["lookback_days"] = 252            # deployed value
    ts_params.setdefault("benchmark", bench)
    print("building ts_momentum reference ...", flush=True)
    ts_sigs = build_signals(ts_spec["kind"], ts_params, dfs)
    ts_wf = psim.simulate_walk_forward(ts_sigs, dfs, start=START, end=END, is_years=3, oos_years=1,
                                       step_years=1, **{k: GATE[k] for k in ("sharpe_min", "max_dd_pct", "n_min",
                                                                             "min_window_sharpe", "min_window_pass_rate")},
                                       **LIVE)
    ts_ret = ts_wf.aggregate.equity_curve.pct_change().dropna()
    print(f"ts_momentum ref: agg Sharpe {ts_wf.aggregate.sharpe_annualised:.2f}", flush=True)

    lines = ["# event_earnings_drift Stage A — validation chain steps 2-6 (2026-08-06)", "",
             "Step 1 (run_spec two-clause + enforced DSR vs 91 trials) PASSED for both combos below.",
             "This artifact runs steps 2-6 with the retirement-grade net-of-cost machinery.", ""]
    results = {}
    for combo in PASSERS:
        label = combo["label"]
        params = {**FIXED, **{k: v for k, v in combo.items() if k != "label"}}
        print(f"\n=== {label} ===", flush=True)
        sigs = build_signals(spec["kind"], params, dfs)
        print(f"{len(sigs)} signals", flush=True)

        # step 2a — net_gate_rerun replica (FULL + last-30% OOS split, default cost model)
        oos_start = START + timedelta(days=int((END - START).days * 0.70))
        full = psim.simulate(sigs, dfs, sharpe_min=GATE["sharpe_min"], max_dd_pct=GATE["max_dd_pct"], n_min=GATE["n_min"])
        oos = psim.simulate([s for s in sigs if s.fill_date >= oos_start], dfs,
                            sharpe_min=GATE["sharpe_min"], max_dd_pct=GATE["max_dd_pct"], n_min=GATE["n_min"])

        # step 2b — net-of-cost walk-forward, LIVE fill config (the binding standard)
        wf = psim.simulate_walk_forward(sigs, dfs, start=START, end=END, is_years=3, oos_years=1, step_years=1,
                                        sharpe_min=GATE["sharpe_min"], max_dd_pct=GATE["max_dd_pct"],
                                        n_min=GATE["n_min"], min_window_sharpe=GATE["min_window_sharpe"],
                                        min_window_pass_rate=GATE["min_window_pass_rate"], **LIVE)
        a = wf.aggregate
        win = " ".join(f"{sp.in_sample_end.year}:{r.sharpe_annualised:.2f}"
                       f"{'v' if r.sharpe_annualised > 0.5 else 'x'}" for sp, r in wf.window_results)

        # step 3 — DSR on the net walk-forward aggregate curve
        d = dsr_of(a.equity_curve)

        # step 4 — correlation vs ts_momentum (aligned daily equity returns)
        my_ret = a.equity_curve.pct_change().dropna()
        corr = float(pd.concat([my_ret, ts_ret], axis=1, join="inner").corr().iloc[0, 1])

        # step 6 — fill-rate / filled-vs-missed guard (spec §2)
        fill_ok = full.fill_rate >= 0.60
        adverse = (full.avg_missed_fwd_return or 0) > 2 * max(full.avg_filled_fwd_return or 0, 1e-9)
        guard = "PASS" if (fill_ok and not adverse) else "EXECUTION-MODEL MISMATCH"

        step2_pass = bool(wf.aggregate_gate_passed and wf.window_clause_passed and oos.deployment_gate_passed)
        chain_pass = bool(step2_pass and d > 0.95 and guard == "PASS")
        results[label] = dict(full=full, oos=oos, wf=wf, dsr=d, corr=corr, guard=guard, chain=chain_pass)

        lines += [f"## {label}", "",
                  f"- signals {len(sigs)}",
                  f"- **2a net split replica**: FULL Sharpe {full.sharpe_annualised:.2f} / |MDD| {abs(full.max_drawdown_pct):.1f}% / n {full.n_trades} / fill {full.fill_rate*100:.0f}% -> {'PASS' if full.deployment_gate_passed else 'FAIL'} · OOS(30%) Sharpe {oos.sharpe_annualised:.2f} / |MDD| {abs(oos.max_drawdown_pct):.1f}% / n {oos.n_trades} -> {'PASS' if oos.deployment_gate_passed else 'FAIL'}",
                  f"- **2b net walk-forward (LIVE fills)**: agg Sharpe **{a.sharpe_annualised:.2f}** / |MDD| {abs(a.max_drawdown_pct):.1f}% / n {a.n_trades} / fill {a.fill_rate*100:.0f}% · aggregate {'PASS' if wf.aggregate_gate_passed else 'FAIL'} · per-window {wf.n_windows_above_floor}/{wf.n_windows} {'PASS' if wf.window_clause_passed else 'FAIL'}",
                  f"  - windows: {win}",
                  f"- **3 DSR (net curve, N=91)**: **{d:.3f}** -> {'PASS' if d > 0.95 else 'FAIL'}",
                  f"- **4 corr vs ts_momentum**: **{corr:+.2f}**",
                  f"- **6 fill guard**: fill {full.fill_rate*100:.0f}% · filled fwd {100*(full.avg_filled_fwd_return or 0):.2f}% vs missed fwd {100*(full.avg_missed_fwd_return or 0):.2f}% -> **{guard}**",
                  f"- **CHAIN (2-4,6)**: **{'PASS -> run step 5 CPCV' if chain_pass else 'FAIL -> retire per kill rule'}**", ""]

    out = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-06-earnings-drift-chain-steps2-6.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nwrote {out}")
    for label, r in results.items():
        print(f"{label}: chain {'PASS' if r['chain'] else 'FAIL'} (wf {r['wf'].aggregate.sharpe_annualised:.2f}, "
              f"dsr {r['dsr']:.3f}, corr {r['corr']:+.2f}, guard {r['guard']})")


if __name__ == "__main__":
    main()
