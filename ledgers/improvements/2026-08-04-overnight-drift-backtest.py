"""Strategy #1 — OVERNIGHT DRIFT (close->open hold), suggest-only research backtest.
Claude1 paper-research pilot, 2026-08-04.

Hypothesis: equities earn most of their return OVERNIGHT (close->open) and ~zero
intraday (the "overnight anomaly"; Cooper-Cliff-Gulen 2008, Lachance 2021). Tradeable
form: hold long overnight, flat during the day. Each day = buy at prior close (MOC),
sell at today's open (MOO) = ONE round trip PER DAY -> very high turnover, so the
whole question is whether the edge survives cost.

This is a RETURN-SERIES strategy (not a swing entry/stop/target), so it is gated
directly from a daily net-return equity curve using the SAME gate the project applies:
  Sharpe > 1.0 (annualized) AND |MaxDD| < 25% AND >=50% of the 6 OOS years clear
  Sharpe > 0.5 AND Deflated Sharpe (Bailey-Lopez de Prado) > 0.95, ALL NET OF COST.

Reads ONLY the cached daily bars + the repo's sharpe_stats. Writes ONLY its own
report under ledgers/improvements/. Touches no strategy code / deployable set / orders.

Usage:  uv run python ledgers/improvements/2026-08-04-overnight-drift-backtest.py
"""
from __future__ import annotations
import os, sys, math
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

import numpy as np
import pandas as pd
from tools.backtest import data_cache, sharpe_stats as ss

INSTRUMENTS = ["SPY", "QQQ", "IWM"]
START, END = "2017-01-01", "2026-05-25"
OOS_YEARS = [2020, 2021, 2022, 2023, 2024, 2025]
COSTS_BPS = [0.0, 3.0, 5.0]          # round-trip cost per day (buy MOC + sell MOO)
GATE = dict(sharpe_min=1.0, mdd_max=25.0, win_sharpe=0.5, win_rate=0.5, dsr_min=0.95)


def overnight_returns(ticker: str) -> pd.Series:
    """close[t-1] -> open[t] return series, indexed by date t."""
    df = data_cache.load(ticker)
    df = df.loc[START:END]
    o = df["Open"].astype(float); c = df["Close"].astype(float)
    r = (o / c.shift(1)) - 1.0
    return r.dropna()


def ann_sharpe(r: pd.Series) -> float:
    if r.std(ddof=1) == 0 or len(r) < 2: return 0.0
    return float(r.mean() / r.std(ddof=1) * math.sqrt(252))


def max_dd_pct(r: pd.Series) -> float:
    eq = (1.0 + r).cumprod()
    peak = eq.cummax()
    return float(((eq / peak) - 1.0).min() * 100.0)


def dsr(r: pd.Series, n_trials: int, var_trials: float) -> float:
    m = ss.sharpe_moments(list(r.values))
    out = ss.deflated_sharpe(m["sr"], int(m["n"]), m["skew"], m["kurt_raw"],
                             var_trials=max(var_trials, 1e-12), n_trials=max(n_trials, 1))
    return float(out["dsr"])


def evaluate(r_gross: pd.Series, cost_bps: float, n_trials: int, var_trials: float) -> dict:
    r = r_gross - cost_bps / 1e4          # subtract round-trip cost each day
    full_s = ann_sharpe(r); mdd = max_dd_pct(r)
    cum = float((1.0 + r).prod() - 1.0)
    per_win = {}
    for y in OOS_YEARS:
        ry = r[(r.index >= f"{y}-01-01") & (r.index <= f"{y}-12-31")]
        per_win[y] = ann_sharpe(ry) if len(ry) > 20 else float("nan")
    n_clear = sum(1 for y in OOS_YEARS if per_win[y] > GATE["win_sharpe"])
    win_rate = n_clear / len(OOS_YEARS)
    d = dsr(r, n_trials, var_trials)
    gate = (full_s > GATE["sharpe_min"] and abs(mdd) < GATE["mdd_max"]
            and win_rate >= GATE["win_rate"] and d > GATE["dsr_min"])
    return dict(cost_bps=cost_bps, sharpe=full_s, mdd=mdd, cum=cum, n=len(r),
                per_win=per_win, n_clear=n_clear, win_rate=win_rate, dsr=d, gate=gate)


def main() -> None:
    series = {t: overnight_returns(t) for t in INSTRUMENTS}
    # equal-weight basket (mean across instruments each day)
    basket = pd.concat(series.values(), axis=1).mean(axis=1).dropna()
    universe = dict(series); universe["BASKET(SPY,QQQ,IWM)"] = basket

    # DSR bookkeeping: the number of variants we tried (instruments x costs) is the
    # multiple-testing count; var of their gross annualized Sharpes is var_trials.
    gross_sharpes = [ann_sharpe(s) for s in universe.values()]
    n_trials = len(universe) * len([c for c in COSTS_BPS if c > 0])
    var_trials = float(np.var([ss.annualized_to_per_period(x) for x in gross_sharpes], ddof=1)) if len(gross_sharpes) > 1 else 1e-6

    lines = ["# Strategy #1 — Overnight drift (close->open hold) — net-of-cost gate (pilot, 2026-08-04)", "",
             f"Universe: {', '.join(INSTRUMENTS)} + equal-weight basket. Period {START}..{END}. "
             f"Signal: hold long overnight (buy prior close, sell today open), flat intraday. "
             f"Round-trip cost charged PER DAY. Gate: Sharpe>1.0 & |MDD|<25% & >=50% of 6 OOS "
             f"years clear Sharpe>0.5 & Deflated Sharpe>0.95, ALL net of cost.", "",
             "| instrument | cost bps | net Sharpe | |MDD|% | cum% | OOS yrs>0.5 | DSR | GATE |",
             "|---|---|---|---|---|---|---|---|"]
    results = {}
    for name, s in universe.items():
        for c in COSTS_BPS:
            r = evaluate(s, c, n_trials, var_trials)
            results[(name, c)] = r
            lines.append(f"| {name} | {c:.0f} | {r['sharpe']:.2f} | {abs(r['mdd']):.1f} | "
                         f"{r['cum']*100:.0f} | {r['n_clear']}/6 | {r['dsr']:.2f} | "
                         f"{'PASS' if r['gate'] else 'FAIL'} |")
    # per-window detail for the headline (basket) at realistic 3bps
    b3 = results[("BASKET(SPY,QQQ,IWM)", 3.0)]
    lines += ["", "## Per-year OOS Sharpe — basket @ 3bps (realistic)",
              "| " + " | ".join(str(y) for y in OOS_YEARS) + " |",
              "|" + "---|" * len(OOS_YEARS),
              "| " + " | ".join(f"{b3['per_win'][y]:.2f}" for y in OOS_YEARS) + " |"]
    # verdict
    any_pass = any(r["gate"] for (n, c), r in results.items() if c > 0)
    spy_gross = results[("SPY", 0.0)]["sharpe"]; spy_net3 = results[("SPY", 3.0)]["sharpe"]
    lines += ["", "## Verdict",
              f"- Gross (0bps) overnight Sharpe is real (SPY {spy_gross:.2f}) but the edge is "
              f"~a few bps/day, so daily round-trip cost is the whole game.",
              f"- SPY: gross {spy_gross:.2f} -> net@3bps {spy_net3:.2f}.",
              f"- **{'AT LEAST ONE net variant PASSES the gate.' if any_pass else 'NO net-of-cost variant clears the gate.'}**",
              "- Suggest-only. Nothing applied."]
    out_md = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-04-overnight-drift-backtest.md")
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    # console
    for (name, c), r in results.items():
        if c in (0.0, 3.0):
            print(f"{name:22} @{c:.0f}bps: Sharpe={r['sharpe']:.2f} |MDD|={abs(r['mdd']):.1f}% "
                  f"OOS {r['n_clear']}/6 DSR={r['dsr']:.2f} -> {'PASS' if r['gate'] else 'FAIL'}")
    print(f"\nANY net-cost variant passes gate: {any_pass}")
    print(f"wrote {out_md}")


if __name__ == "__main__":
    main()
