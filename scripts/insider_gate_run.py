"""Phase 6b — run the event_insider_buying KIND through the hardened gate.

Loads the candidate spec, replays the precomputed insider-events file into
TradeSignals, and judges them through the Phase 1 net-of-cost portfolio
simulator (net cost + OHLC fills + cap-weight) over the full period and an OOS
(last 30%) split — the SAME gate that retired 4/5 generic setups on 2026-06-17.

Verdict: PROMOTE (paper) only if BOTH full and OOS clear Sharpe>1.0 AND
|MDD|<25% AND n>=30, net of cost. Otherwise RETIRE. This is the arbiter the
epic set: "fails the gate → retire is a valid, expected outcome." Do not tune.

Read-only: writes a markdown verdict report; changes no config.

Usage:  uv run python scripts/insider_gate_run.py [--spec PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from tools.backtest import portfolio_simulator as psim
from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

DEFAULT_SPEC = "tools/quant_strategies/event_insider_buying.yml"


def _tot_ret(res) -> float:
    ec = res.equity_curve
    return (float(ec.iloc[-1]) / float(ec.iloc[0]) - 1.0) if len(ec) else 0.0


def run(spec_path: str) -> dict:
    spec = yaml.safe_load(open(spec_path, encoding="utf-8"))
    kind = spec["kind"]
    kind_mod = KIND_REGISTRY[kind]
    params = dict(spec.get("params", {}))
    benchmark = spec["universe"]["benchmark"]
    params.setdefault("benchmark", benchmark)

    tickers = resolve_universe_tickers(spec)
    if benchmark not in tickers:
        tickers.append(benchmark)
    start = date.fromisoformat(str(spec["period"]["start"]))
    end = date.fromisoformat(str(spec["period"]["end"]))

    dfs = _load_universe(tickers, start, end, force_refetch=False)
    state = kind_mod.precompute(dfs, params)

    n_event_tickers = len(getattr(state, "events_by_ticker", {}) or {})
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

    return {
        "kind": kind, "n_event_tickers": n_event_tickers, "n_signals": len(signals),
        "gate": (smin, ddmax, nmin),
        "full": {"sharpe": full.sharpe_annualised, "mdd": full.max_drawdown_pct,
                 "n": full.n_trades, "fill": full.fill_rate, "ret": _tot_ret(full),
                 "filled_fwd": full.avg_filled_fwd_return, "missed_fwd": full.avg_missed_fwd_return,
                 "pass": full.deployment_gate_passed},
        "oos": {"sharpe": oos.sharpe_annualised, "mdd": oos.max_drawdown_pct,
                "n": oos.n_trades, "fill": oos.fill_rate, "ret": _tot_ret(oos),
                "pass": oos.deployment_gate_passed},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default=DEFAULT_SPEC)
    ap.add_argument("--out", default="journal/backtest/insider-kind-gate.md")
    args = ap.parse_args()

    r = run(args.spec)
    f, o = r["full"], r["oos"]
    smin, ddmax, nmin = r["gate"]
    promote = f["pass"] and o["pass"]
    verdict = "PROMOTE (paper)" if promote else "RETIRE"

    lines = [
        "# Insider KIND — net-of-cost gate verdict (Phase 6b)", "",
        f"- Kind: `{r['kind']}`  ·  event tickers: {r['n_event_tickers']}  ·  signals: {r['n_signals']}",
        f"- Gate: Sharpe>{smin} AND |MDD|<{ddmax}% AND n>={nmin}, net of cost, on BOTH full + OOS", "",
        "| window | net Sharpe | MDD% | n | fill% | total ret% | pass |",
        "|---|---|---|---|---|---|---|",
        f"| FULL | {f['sharpe']:.2f} | {f['mdd']:.1f} | {f['n']} | {f['fill']*100:.0f} | {f['ret']*100:.1f} | {f['pass']} |",
        f"| OOS (last 30%) | {o['sharpe']:.2f} | {o['mdd']:.1f} | {o['n']} | {o['fill']*100:.0f} | {o['ret']*100:.1f} | {o['pass']} |",
        "",
        f"- Adverse-selection check (full): filled fwd {f['filled_fwd']*100:.2f}% vs "
        f"missed fwd {f['missed_fwd']*100:.2f}%",
        "",
        f"## VERDICT: **{verdict}**",
    ]
    out = args.out
    if not os.path.isabs(out):
        out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"signals={r['n_signals']} | FULL S={f['sharpe']:.2f} MDD={f['mdd']:.1f} "
          f"n={f['n']} | OOS S={o['sharpe']:.2f} n={o['n']} -> {verdict}", flush=True)
    print(f"wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
