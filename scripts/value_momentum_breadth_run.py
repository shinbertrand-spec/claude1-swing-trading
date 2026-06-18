"""BREADTH experiment on the (retired) integrated value+momentum KIND.

Question this answers (raised by the retirement commit 7cad963 itself):
  Was value+momentum retired because the IDEA is dead, or because the
  8-position cap + banding STARVED a cross-sectional factor of breadth
  (n=13 entries / 9y)? The Fundamental Law of Active Management says
  IR ~= IC * sqrt(breadth) -- so a real-but-small IC at breadth 8 produces
  a near-zero information ratio by construction.

Method (single isolated variable = BREADTH):
  Re-run the SAME KIND, SAME universe / period / costs / bands, sweeping
  K in {8, 20, 40}. At each K the held-set is the banded top-K and the
  portfolio simulator's concurrency cap AND per-name size move together:
  max_positions = K, max_pct_per_position = 1/K (equal-weight target; the
  ADV tilt still applies on top, as in the live sim).

Read the verdict as a TREND:
  * If integrated Sharpe / n / t-stat rise materially with K and clear the
    gate at K=40 -> the CAP was the killer, not the signal. That justifies
    a per-track position cap for the factor sleeve.
  * If they stay flat/weak across K -> the idea is genuinely dead at our
    cost structure; 8 stays, question closed.

This does NOT change any production default. It writes a report only.
Stale on-disk SEC facts are used deliberately (PIT logic ignores any filing
with filed > asof, so cache age is irrelevant to a historical backtest).

Usage:  uv run python scripts/value_momentum_breadth_run.py
        [--breadths 8,20,40] [--spec PATH] [--out PATH] [--no-value]
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from tools.backtest import portfolio_simulator as psim
from tools.fundamentals import pit_fundamentals as pf
from tools.quant_strategies._kinds import value_momentum_integrated as vmi
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

# Use the on-disk SEC facts cache regardless of its 24h TTL. Correct for a
# historical PIT backtest: pit_fundamentals filters filed<=asof, so a newer
# filing would never enter any in-period rebalance.
pf.CACHE_TTL_SECONDS = 10 ** 12

DEFAULT_SPEC = "tools/quant_strategies/value_momentum_integrated.yml"


def _tot_ret(res) -> float:
    ec = res.equity_curve
    return (float(ec.iloc[-1]) / float(ec.iloc[0]) - 1.0) if len(ec) else 0.0


def _is_tstat(trades, oos_start: date) -> tuple[float, int]:
    rs = [t.net_return for t in trades if t.fill_date < oos_start]
    n = len(rs)
    if n < 2:
        return 0.0, n
    mu = sum(rs) / n
    sd = math.sqrt(sum((r - mu) ** 2 for r in rs) / (n - 1))
    if sd == 0:
        return 0.0, n
    return mu / (sd / math.sqrt(n)), n


def _build_facts(tickers: list[str]) -> dict:
    cik_map = pf.load_ticker_cik_map()
    out, miss = {}, 0
    for t in tickers:
        cik = cik_map.get(t)
        if not cik:
            miss += 1
            continue
        try:
            out[t] = pf.fetch_company_facts(cik)
        except Exception:        # noqa: BLE001
            miss += 1
    print(f"  facts: {len(out)}/{len(tickers)} have company-facts ({miss} missing)", flush=True)
    return out


def run_variant(name, value_w, K, dfs, params_base, facts, gate, oos_start):
    """One (variant x breadth) cell. Banded top-K held-set + K-wide sim cap +
    equal-weight (1/K) target size."""
    params = dict(params_base)
    params["value_weight"] = value_w
    params["momentum_weight"] = 1.0 - value_w
    params["top_k"] = K
    if value_w != 0:
        params["_facts_by_ticker"] = facts
    benchmark = params.get("benchmark")

    cfg = psim.PortfolioConfig(max_positions=K, max_pct_per_position=1.0 / K)

    state = vmi.precompute(dfs, params)
    signals = []
    for t, df in dfs.items():
        if t == benchmark:
            continue
        signals.extend(vmi.replay(df, t, params, state))

    smin, ddmax, nmin = gate
    oos_sigs = [s for s in signals if s.fill_date >= oos_start]
    full = psim.simulate(signals, dfs, cfg, sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin)
    oos = psim.simulate(oos_sigs, dfs, cfg, sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin)
    tstat, n_is = _is_tstat(full.trades, oos_start)
    return {
        "name": name, "K": K, "n_signals": len(signals),
        "n_names": len(state.spans_by_ticker), "tstat_is": tstat, "n_is": n_is,
        "full_row": (full.sharpe_annualised, full.max_drawdown_pct, full.n_trades,
                     full.fill_rate, _tot_ret(full), full.deployment_gate_passed),
        "oos_row": (oos.sharpe_annualised, oos.max_drawdown_pct, oos.n_trades,
                    oos.fill_rate, _tot_ret(oos), oos.deployment_gate_passed),
    }


def run(spec_path: str, breadths: list[int], use_value: bool) -> dict:
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

    print(f"loading {len(tickers)} tickers {start}..{end} (cached) ...", flush=True)
    dfs = _load_universe(tickers, start, end, force_refetch=False)
    print(f"loaded {len(dfs)} price frames", flush=True)

    facts = {}
    if use_value:
        print("loading SEC company-facts (on-disk cache, TTL overridden) ...", flush=True)
        facts = _build_facts([t for t in tickers if t != benchmark])

    variants = [("integrated", 0.5), ("momentum_only", 0.0)]
    if use_value:
        variants.append(("value_only", 1.0))

    cells = []
    for name, vw in variants:
        if vw != 0 and not use_value:
            continue
        for K in breadths:
            print(f"running {name} K={K} ...", flush=True)
            cells.append(run_variant(name, vw, K, dfs, params_base, facts, gate, oos_start))
    return {"spec": spec_path, "gate": gate, "oos_start": oos_start, "cells": cells}


def _fmt(r: dict) -> str:
    smin, ddmax, nmin = r["gate"]
    lines = ["# Value+Momentum -- BREADTH experiment (cap-killed vs idea-dead)", "",
             f"- Spec: `{r['spec']}`  --  gate: net Sharpe>{smin} AND |MDD|<{ddmax}% AND n>={nmin}, full+OOS",
             f"- OOS split: fills on/after {r['oos_start']} (last 30%)",
             "- Single isolated variable = breadth K (held-set top-K, sim cap=K, size=1/K)",
             "- Baseline retired form was K=8. The retirement flagged 'n=13 entries / 9y'.",
             "",
             "| variant | K | window | net Sharpe | MDD% | n | held names | IS t (n) | pass |",
             "|---|---|---|---|---|---|---|---|---|"]
    for c in r["cells"]:
        for win, row in (("full", c["full_row"]), ("OOS", c["oos_row"])):
            t_cell = f"{c['tstat_is']:.2f} ({c['n_is']})" if win == "full" else ""
            lines.append(
                f"| {c['name']} | {c['K']} | {win} | {row[0]:.2f} | {row[1]:.1f} | "
                f"{row[2]} | {c['n_names'] if win=='full' else ''} | {t_cell} | {row[5]} |")
    lines += ["", "## How to read this", "",
              "- Integrated **n_trades / IS t-stat rising with K** => the 8-cap (not the",
              "  signal) was the binding constraint; a per-track factor cap is justified.",
              "- Integrated **flat/weak Sharpe across K, still failing the gate at K=40** =>",
              "  the idea is dead at our cost structure; keep 8, close the question.",
              "- Note: deeper K dilutes per-name alpha (further down the ranked band), so a",
              "  modest Sharpe dip with much higher n+t can still be the *better* evidence",
              "  the edge is real -- breadth trades concentration for significance.", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default=DEFAULT_SPEC)
    ap.add_argument("--breadths", default="8,20,40")
    ap.add_argument("--out", default="journal/backtest/value-momentum-breadth.md")
    ap.add_argument("--no-value", action="store_true",
                    help="momentum-only sweep (skip the SEC fundamentals load)")
    args = ap.parse_args()

    breadths = [int(x) for x in args.breadths.split(",") if x.strip()]
    r = run(args.spec, breadths, use_value=not args.no_value)
    report = _fmt(r)

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
