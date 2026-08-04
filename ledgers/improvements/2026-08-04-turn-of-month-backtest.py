"""Strategy #4 — TURN-OF-MONTH seasonality, suggest-only research backtest.
Claude1 paper-research pilot, 2026-08-04.

Hypothesis: equity returns cluster around the turn of the month (Lakonishok-Smidt 1988):
be long the last trading day of the month + the first 3 trading days of the next month,
flat otherwise. LOW turnover (~1 round trip / month) = cost-friendly, unlike overnight.

Return-series strategy, gated directly from the daily net-return curve with the SAME
gate: Sharpe>1.0 (annualized on the daily series) & |MDD|<25% & >=50% of 6 OOS years
clear Sharpe>0.5 & Deflated Sharpe>0.95, NET OF COST (cost charged on each in/out flip).

Usage:  uv run python ledgers/improvements/2026-08-04-turn-of-month-backtest.py
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
LAST_N, FIRST_N = 1, 3            # last 1 trading day of month + first 3 of next
COST_BPS = [0.0, 5.0, 10.0]      # per in/out FLIP (buy at window start, sell at window end)
GATE = dict(sharpe_min=1.0, mdd_max=25.0, win_sharpe=0.5, win_rate=0.5, dsr_min=0.95)


def in_market_mask(idx: pd.DatetimeIndex) -> pd.Series:
    """True on the last LAST_N trading days of each month + first FIRST_N of the month."""
    s = pd.Series(idx, index=idx)
    ym = s.dt.to_period("M")
    mask = pd.Series(False, index=idx)
    for _, grp in s.groupby(ym):
        days = list(grp.index)
        for d in days[:FIRST_N]:      # first N of month
            mask.loc[d] = True
        for d in days[-LAST_N:]:      # last N of month
            mask.loc[d] = True
    return mask


def strat_returns(ticker: str, cost_bps: float) -> pd.Series:
    df = data_cache.load(ticker).loc[START:END]
    ret = df["Close"].astype(float).pct_change().fillna(0.0)
    mask = in_market_mask(df.index)
    r = ret.where(mask, 0.0)
    flips = mask.astype(int).diff().abs().fillna(0)   # 1 on each enter or exit day
    r = r - flips * (cost_bps / 1e4)
    return r


def ann_sharpe(r):
    return float(r.mean()/r.std(ddof=1)*math.sqrt(252)) if r.std(ddof=1) > 0 and len(r) > 2 else 0.0

def max_dd(r):
    eq = (1+r).cumprod(); return float(((eq/eq.cummax())-1).min()*100)

def dsr(r, n_trials, var_trials):
    m = ss.sharpe_moments(list(r.values))
    return float(ss.deflated_sharpe(m["sr"], int(m["n"]), m["skew"], m["kurt_raw"],
                                    var_trials=max(var_trials,1e-12), n_trials=n_trials)["dsr"])


def evaluate(r, n_trials, var_trials):
    full = ann_sharpe(r); mdd = max_dd(r); cum = float((1+r).prod()-1)
    per = {}
    for y in OOS_YEARS:
        ry = r[(r.index >= f"{y}-01-01") & (r.index <= f"{y}-12-31")]
        per[y] = ann_sharpe(ry) if len(ry) > 20 else float("nan")
    nclear = sum(1 for y in OOS_YEARS if per[y] > GATE["win_sharpe"])
    d = dsr(r, n_trials, var_trials)
    gate = full > GATE["sharpe_min"] and abs(mdd) < GATE["mdd_max"] and nclear/6 >= GATE["win_rate"] and d > GATE["dsr_min"]
    return dict(sharpe=full, mdd=mdd, cum=cum, per=per, nclear=nclear, dsr=d, gate=gate, n=len(r))


def main():
    series = {t: {c: strat_returns(t, c) for c in COST_BPS} for t in INSTRUMENTS}
    basket = {c: pd.concat([series[t][c] for t in INSTRUMENTS], axis=1).mean(axis=1) for c in COST_BPS}
    universe = {t: series[t] for t in INSTRUMENTS}; universe["BASKET"] = basket
    gross = [ann_sharpe(universe[n][0.0]) for n in universe]
    var_trials = float(np.var([ss.annualized_to_per_period(x) for x in gross], ddof=1)) if len(gross) > 1 else 1e-6
    n_trials = len(universe)*len([c for c in COST_BPS if c > 0])
    lines = ["# Strategy #4 — Turn-of-month seasonality — net-of-cost gate (pilot, 2026-08-04)", "",
             f"Universe {INSTRUMENTS}+basket. {START}..{END}. Long last {LAST_N} + first {FIRST_N} trading days/month, "
             f"flat else. Cost charged per enter/exit flip. Gate: Sharpe>1.0 & |MDD|<25% & >=50% of 6 OOS yrs "
             f"clear Sharpe>0.5 & DSR>0.95, net.", "",
             "| instrument | cost bps | net Sharpe | |MDD|% | cum% | OOS yrs>0.5 | DSR | GATE |",
             "|---|---|---|---|---|---|---|---|"]
    results = {}
    for name, bycost in universe.items():
        for c in COST_BPS:
            r = evaluate(bycost[c], n_trials, var_trials); results[(name, c)] = r
            lines.append(f"| {name} | {c:.0f} | {r['sharpe']:.2f} | {abs(r['mdd']):.1f} | {r['cum']*100:.0f} | "
                         f"{r['nclear']}/6 | {r['dsr']:.2f} | {'PASS' if r['gate'] else 'FAIL'} |")
    anypass = any(r["gate"] for (n, c), r in results.items() if c > 0)
    lines += ["", f"## Verdict: **{'AT LEAST ONE net variant PASSES' if anypass else 'NO net variant clears the gate'}**",
              "- Low-turnover so cost is not the killer here; the question is whether the seasonal Sharpe is high enough + robust across years.",
              "- Suggest-only; nothing applied."]
    out = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-04-turn-of-month-backtest.md")
    with open(out, "w", encoding="utf-8") as fh: fh.write("\n".join(lines)+"\n")
    for (name, c), r in results.items():
        if c in (0.0, 5.0):
            print(f"{name:8} @{c:.0f}bps: Sharpe={r['sharpe']:.2f} |MDD|={abs(r['mdd']):.1f}% OOS {r['nclear']}/6 DSR={r['dsr']:.2f} -> {'PASS' if r['gate'] else 'FAIL'}")
    print(f"\nANY net variant passes: {anypass}\nwrote {out}")


if __name__ == "__main__":
    main()
