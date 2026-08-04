"""Gap-fade DAILY PROXY (Stage 0 of the paid-feed scope) — suggest-only research.
Claude1 paper-research pilot, 2026-08-04.

The hourly probe flagged HIGH-vol gap-fade as the one intraday mechanism with a pulse
(+0.17%/trade net @5bps, t=1.30). Before spending on a paid intraday feed to test it
properly, run the FREE daily-bar proxy that captures the same economics on data we
already have (2017-2026) and put it through the REAL gate.

Gap-fade is a single-day strategy: on a down-gap open, buy and exit same day at the
close. The ONLY part needing intraday data is entry timing (open vs first-hour close).
The open->close leg is fully expressible on daily bars. So:
  - Stage 0 (this script, $0): entry = OPEN, exit = CLOSE, gated 2017-2026.
  - Stage 1 (paid feed, only if Stage 0 warrants): refine entry to first-hour close.

Return-series strategy (like the turn-of-month / overnight-drift probes): each day, the
strategy return = equal-weight mean of same-day (close/open - 1) across all names whose
open gapped down >= GAP%, minus cost, else 0 (flat). Gated on the daily curve:
Sharpe>1.0 & |MDD|<25% & >=50% of 6 OOS yrs clear Sharpe>0.5 & Deflated Sharpe>0.95, NET.

IMPORTANT honesty note: gap-down opens have WIDE spreads exactly when this trades, so a
flat bps cost UNDERSTATES real cost. We bracket 5/10/20 bps; the true number needs the
intraday feed (Stage 1). The daily proxy is therefore an OPTIMISTIC upper bound.

Usage:  uv run python ledgers/improvements/2026-08-04-gap-fade-daily-proxy-backtest.py
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
from tools.quant_strategies._universe import resolve_universe_tickers

SPEC = {"universe": {"name": "sp500_leaning_88", "benchmark": "SPY"}}
START, END = "2017-01-01", "2026-05-25"
OOS_YEARS = [2020, 2021, 2022, 2023, 2024, 2025]
GAP_THRESHES = [1.0, 2.0, 3.0]        # % down-gap to trigger the fade
COSTS_BPS = [5.0, 10.0, 20.0]         # round-trip; wide because gap-open spreads are wide
TREND_FILTER = True                    # long-only quality: only fade names above their 200d SMA
GATE = dict(sharpe_min=1.0, mdd_max=25.0, win_sharpe=0.5, win_rate=0.5, dsr_min=0.95)


def load_all(tickers):
    out = {}
    for t in tickers:
        try:
            df = data_cache.load(t).loc[START:END]
            if len(df) > 250:
                out[t] = df
        except Exception:
            pass
    return out


def strat_returns(dfs, gap_pct, cost_bps, trend_filter):
    """Daily equal-weight gap-fade return series across the universe."""
    per_name = {}
    for t, df in dfs.items():
        o = df["Open"].astype(float); c = df["Close"].astype(float)
        prev_c = c.shift(1)
        gap = o / prev_c - 1.0
        trig = gap <= -gap_pct / 100.0
        if trend_filter:
            sma200 = c.rolling(200).mean()
            trig = trig & (c > sma200)
        intraday = (c / o - 1.0) - cost_bps / 1e4     # buy open, sell close, net cost
        per_name[t] = intraday.where(trig, np.nan)
    mat = pd.DataFrame(per_name)
    # equal-weight across names that triggered that day; 0.0 (flat) when none triggered
    daily = mat.mean(axis=1, skipna=True).reindex(sorted(set().union(*[df.index for df in dfs.values()])))
    daily = daily.fillna(0.0).sort_index()
    n_trades = int(mat.notna().sum().sum())
    return daily, n_trades


def ann_sharpe(r):
    r = pd.Series(r)
    return float(r.mean() / r.std(ddof=1) * math.sqrt(252)) if r.std(ddof=1) > 0 and len(r) > 2 else 0.0

def max_dd(r):
    eq = (1 + pd.Series(r)).cumprod(); return float(((eq / eq.cummax()) - 1).min() * 100)

def per_trade_t(mat_vals):
    x = np.asarray([v for v in mat_vals if not math.isnan(v)])
    if len(x) < 5 or np.std(x, ddof=1) == 0: return 0.0, 0.0, len(x)
    return float(np.mean(x) / np.std(x, ddof=1) * math.sqrt(len(x))), float((x > 0).mean() * 100), len(x)

def dsr(r, n_trials, var_trials):
    m = ss.sharpe_moments(list(pd.Series(r).values))
    return float(ss.deflated_sharpe(m["sr"], int(m["n"]), m["skew"], m["kurt_raw"],
                                    var_trials=max(var_trials, 1e-12), n_trials=n_trials)["dsr"])


def evaluate(daily, n_trials, var_trials):
    full = ann_sharpe(daily); mdd = max_dd(daily); cum = float((1 + daily).prod() - 1)
    per = {}
    for y in OOS_YEARS:
        ry = daily[(daily.index >= f"{y}-01-01") & (daily.index <= f"{y}-12-31")]
        per[y] = ann_sharpe(ry) if len(ry) > 20 else float("nan")
    nclear = sum(1 for y in OOS_YEARS if per[y] > GATE["win_sharpe"])
    d = dsr(daily, n_trials, var_trials)
    gate = full > GATE["sharpe_min"] and abs(mdd) < GATE["mdd_max"] and nclear / 6 >= GATE["win_rate"] and d > GATE["dsr_min"]
    return dict(sharpe=full, mdd=mdd, cum=cum, per=per, nclear=nclear, dsr=d, gate=gate)


def main():
    tickers = resolve_universe_tickers(SPEC)
    print(f"loading {len(tickers)} tickers ...", flush=True)
    dfs = load_all(tickers)
    print(f"{len(dfs)} loaded", flush=True)
    # build all variants; gross Sharpe spread across variants -> DSR var_trials
    variants = {}
    grosses = []
    for gp in GAP_THRESHES:
        d0, n0 = strat_returns(dfs, gp, 0.0, TREND_FILTER)     # gross for var_trials
        grosses.append(ann_sharpe(d0))
        for cb in COSTS_BPS:
            d, n = strat_returns(dfs, gp, cb, TREND_FILTER)
            variants[(gp, cb)] = (d, n)
    var_trials = float(np.var([ss.annualized_to_per_period(x) for x in grosses], ddof=1)) if len(grosses) > 1 else 1e-6
    n_trials = len(GAP_THRESHES) * len(COSTS_BPS)

    lines = ["# Gap-fade DAILY PROXY (Stage 0) — net-of-cost gate (pilot, 2026-08-04)", "",
             f"Universe sp500_leaning_88 ({len(dfs)} loaded). {START}..{END}. Trend filter (above 200d SMA): "
             f"{TREND_FILTER}. Buy down-gap OPEN, sell same-day CLOSE. Equal-weight across gapping names; flat "
             f"when none gap. Gate: Sharpe>1.0 & |MDD|<25% & >=50% of 6 OOS yrs clear Sharpe>0.5 & DSR>0.95, NET.", "",
             "**Optimistic upper bound: flat bps understates the wide gap-open spread. If this FAILS, the paid "
             "intraday feed cannot rescue it. If it PASSES, Stage 1 (paid feed, real entry timing + real spread) is "
             "justified.**", "",
             "| gap% | cost bps | daily Sharpe | |MDD|% | cum% | OOS yrs>0.5 | DSR | GATE |",
             "|---|---|---|---|---|---|---|---|"]
    results = {}
    for gp in GAP_THRESHES:
        for cb in COSTS_BPS:
            daily, n = variants[(gp, cb)]
            r = evaluate(daily, n_trials, var_trials); results[(gp, cb)] = (r, n)
            lines.append(f"| {gp:.0f} | {cb:.0f} | {r['sharpe']:.2f} | {abs(r['mdd']):.1f} | {r['cum']*100:.0f} | "
                         f"{r['nclear']}/6 | {r['dsr']:.2f} | {'PASS' if r['gate'] else 'FAIL'} |")
    anypass = any(r["gate"] for (r, n) in results.values())
    # per-trade color at gap=2%, cost=10bps
    dref, _ = strat_returns(dfs, 2.0, 10.0, TREND_FILTER)
    lines += ["", f"## Verdict: **{'AT LEAST ONE variant clears the gate -> Stage 1 (paid feed) JUSTIFIED' if anypass else 'NO variant clears the gate -> Stage 1 (paid feed) NOT justified'}**",
              f"- Trades/yr are episodic (fires only on down-gap days), so the full-series Sharpe (with flat days) is a "
              f"conservative capital-efficiency read; even so, the gate is the gate.",
              "- Suggest-only; nothing applied."]
    out = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-04-gap-fade-daily-proxy-backtest.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nDSR n_trials={n_trials} var_trials={var_trials:.2e}")
    for gp in GAP_THRESHES:
        for cb in COSTS_BPS:
            r, n = results[(gp, cb)]
            print(f"gap>={gp:.0f}% @{cb:.0f}bps: n={n:5} Sharpe={r['sharpe']:.2f} |MDD|={abs(r['mdd']):.1f}% "
                  f"OOS {r['nclear']}/6 DSR={r['dsr']:.2f} -> {'PASS' if r['gate'] else 'FAIL'}")
    print(f"\nANY variant passes: {anypass}\nwrote {out}")


if __name__ == "__main__":
    main()
