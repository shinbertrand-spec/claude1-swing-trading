"""A/B the Antonacci market-regime overlay on the live ts_momentum survivor.

ts_momentum_liquid_us is the SOLE surviving generic deployable (net OOS Sharpe
~1.04). It already has per-name absolute momentum; this asks whether adding a
MARKET-level absolute-momentum crash switch (ts_momentum_gem) reduces |max
drawdown| without giving back the Sharpe — the |MDD| line is what retired
clenow_momentum (25.87% > 25%).

Single isolated variable = the regime gate. Baseline is ts_momentum_gem with the
gate DISABLED (provably identical selection to plain ts_momentum); the treatment
turns the gate on with a 12-month (252d) SPY absolute-momentum switch. Same
universe / period / costs / cap / sizing via the hardened net-of-cost simulator.

Read the verdict:
  * Gate-ON cuts |MDD| materially AND keeps net Sharpe >= baseline (full+OOS) AND
    n stays >= 30  -> the overlay earns a doctrine conversation (it is a new RULE,
    see the KIND's DOCTRINE NOTE — not an auto-deploy).
  * Gate-ON guts n (<30) or trims Sharpe for little |MDD| benefit -> the per-name
    sign gate already captures the crash protection; don't add the switch.

Writes a report only; changes no production default.

Usage:  uv run python scripts/ts_momentum_gem_run.py
        [--lookbacks 126,252] [--regime 252] [--out PATH]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from tools.backtest import portfolio_simulator as psim
from tools.quant_strategies._kinds import ts_momentum_gem as gem
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

SPEC = "tools/quant_strategies/ts_momentum_liquid_us.yml"


def _tot_ret(res) -> float:
    ec = res.equity_curve
    return (float(ec.iloc[-1]) / float(ec.iloc[0]) - 1.0) if len(ec) else 0.0


def run_cell(label, dfs, params, gate, oos_start, benchmark):
    state = gem.precompute(dfs, params)
    signals = []
    for t, df in dfs.items():
        if t == benchmark:
            continue
        signals.extend(gem.replay(df, t, params, state))
    smin, ddmax, nmin = gate
    oos_sigs = [s for s in signals if s.fill_date >= oos_start]
    full = psim.simulate(signals, dfs, sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin)
    oos = psim.simulate(oos_sigs, dfs, sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin)
    return {
        "label": label, "n_signals": len(signals),
        "full_row": (full.sharpe_annualised, full.max_drawdown_pct, full.n_trades,
                     _tot_ret(full), full.deployment_gate_passed),
        "oos_row": (oos.sharpe_annualised, oos.max_drawdown_pct, oos.n_trades,
                    _tot_ret(oos), oos.deployment_gate_passed),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookbacks", default="126,252")
    ap.add_argument("--regime", default="252")
    ap.add_argument("--out", default="journal/backtest/ts-momentum-gem-overlay.md")
    args = ap.parse_args()

    lookbacks = [int(x) for x in args.lookbacks.split(",") if x.strip()]
    regime_lb = int(args.regime)

    spec = yaml.safe_load(open(SPEC, encoding="utf-8"))
    base = dict(spec.get("params", {}))
    base.pop("lookback_days", None)  # set per cell below
    benchmark = spec["universe"]["benchmark"]
    base.setdefault("benchmark", benchmark)
    tickers = resolve_universe_tickers(spec)
    if benchmark not in tickers:
        tickers.append(benchmark)
    start = date.fromisoformat(str(spec["period"]["start"]))
    end = date.fromisoformat(str(spec["period"]["end"]))
    g = spec.get("gate", {})
    gate = (float(g.get("sharpe_min", 1.0)), float(g.get("max_dd_pct", 25.0)),
            int(g.get("n_min", 30)))
    oos_start = start + timedelta(days=int((end - start).days * 0.70))

    print(f"loading {len(tickers)} tickers {start}..{end} (cached) ...", flush=True)
    dfs = _load_universe(tickers, start, end, force_refetch=False)
    print(f"loaded {len(dfs)} price frames", flush=True)

    cells = []
    for lb in lookbacks:
        for gate_on in (False, True):
            p = dict(base)
            p["lookback_days"] = lb
            p["market_regime_gate"] = gate_on
            p["regime_lookback_days"] = regime_lb
            label = (f"lb={lb} GEM-ON(reg={regime_lb})" if gate_on
                     else f"lb={lb} baseline(no gate)")
            print(f"running {label} ...", flush=True)
            cells.append(run_cell(label, dfs, p, gate, oos_start, benchmark))

    smin, ddmax, nmin = gate
    lines = ["# ts_momentum + Antonacci market-regime overlay -- A/B", "",
             f"- Spec: `{SPEC}`  --  gate: net Sharpe>{smin} AND |MDD|<{ddmax}% AND n>={nmin}, full+OOS",
             f"- OOS split: fills on/after {oos_start} (last 30%)",
             f"- Overlay: hold nothing when SPY trailing {regime_lb}d return <= 0; else ts_momentum top-K",
             "- Single isolated variable = the regime gate (baseline = same KIND, gate OFF)",
             "",
             "| cell | window | net Sharpe | MDD% | n | ret% | pass |",
             "|---|---|---|---|---|---|---|"]
    for c in cells:
        for win, row in (("full", c["full_row"]), ("OOS", c["oos_row"])):
            lines.append(f"| {c['label']} | {win} | {row[0]:.2f} | {row[1]:.1f} | "
                         f"{row[2]} | {row[3]*100:.1f} | {row[4]} |")
    lines += ["", "## How to read this", "",
              "- Compare each `GEM-ON` row to the `baseline` row at the SAME lookback.",
              "- Overlay WINS if |MDD| drops materially with net Sharpe >= baseline AND n>=30",
              "  on BOTH windows -> open a doctrine conversation (new RULE, not auto-deploy).",
              "- Overlay LOSES if n falls <30 or Sharpe gives back more than the |MDD| gain ->",
              "  the per-name TSMOM sign gate already supplies the crash protection.", ""]
    report = "\n".join(lines) + "\n"

    out = args.out
    if not os.path.isabs(out):
        out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(report, flush=True)
    print(f"wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
