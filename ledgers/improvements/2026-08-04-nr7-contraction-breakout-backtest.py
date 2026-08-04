"""Strategy #3 — NR7 VOLATILITY-CONTRACTION BREAKOUT (Crabel), suggest-only research.
Claude1 paper-research pilot, 2026-08-04.

Hypothesis: the narrowest daily range in 7 days (NR7) precedes a volatility expansion;
buy the breakout above the NR7 bar's high. A distinct mechanism from trend-rank
(ts_momentum) and new-high breakout (#2) — it keys on range CONTRACTION, not level.
Same net-of-cost walk-forward gate as the live setups (marketable-limit + full spread
+ OHLC fills + 8-cap; rolling 3y-IS/1y-OOS/1y-step). Gate: agg Sharpe>1.0 & |MDD|<25%
& n>=30 AND >=50% OOS windows clear Sharpe>0.5, plus Deflated Sharpe>0.95. NET OF COST.

Usage:  uv run python ledgers/improvements/2026-08-04-nr7-contraction-breakout-backtest.py
"""
from __future__ import annotations
import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from datetime import date
import numpy as np
import pandas as pd
from tools.backtest import portfolio_simulator as psim, sharpe_stats as ss
from tools.backtest.setup_replay import TradeSignal
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

KIND = "nr7_contraction_breakout"
FILL_KIND = "ts_momentum"    # breakout = chase-up marketable fill (fill-routing only)
SPEC = {"universe": {"name": "sp500_leaning_88", "benchmark": "SPY"}}
START, END = date(2017, 1, 1), date(2026, 5, 25)
PARAMS = dict(nr_window=7, atr_period=20, atr_stop_multiple=2.5, max_hold_days=20, cooldown_days=5)
GATE = dict(sharpe_min=1.0, max_dd_pct=25.0, n_min=30, min_window_sharpe=0.5, min_window_pass_rate=0.5)


def _atr(df, i, period):
    if i < period: return None
    h = df["High"].values; l = df["Low"].values; c = df["Close"].values
    trs = [max(h[k]-l[k], abs(h[k]-c[k-1]), abs(l[k]-c[k-1])) for k in range(i-period+1, i+1)]
    return float(np.mean(trs)) if trs else None


def nr7_replay(df, ticker, params):
    if ticker == SPEC["universe"]["benchmark"]: return []
    nw = int(params["nr_window"]); atrp = int(params["atr_period"]); mult = float(params["atr_stop_multiple"])
    mh = int(params["max_hold_days"]); cd = int(params["cooldown_days"])
    high = df["High"].values; low = df["Low"].values; rng = high - low
    idx = list(df.index); sigs = []; last = -10**9
    for i in range(max(nw, atrp), len(df)-1):
        if rng[i] != rng[i-nw+1:i+1].min():        # NR7: today's range is narrowest of last nw days
            continue
        nxt = df.iloc[i+1]
        if not (float(nxt["High"]) > high[i]):      # require breakout above NR7 high next day
            continue
        if i - last < cd: continue
        atr = _atr(df, i, atrp)
        if atr is None or atr <= 0: continue
        entry = max(float(nxt["Open"]), float(high[i]))   # fill at breakout level or gap-up open
        stop = entry - mult * atr
        if stop >= entry or entry <= 0: continue
        last = i
        sigs.append(TradeSignal(
            ticker=ticker, setup_type=FILL_KIND, setup_grade="B",
            entry_date=pd.Timestamp(idx[i]).date(), fill_date=pd.Timestamp(idx[i+1]).date(),
            entry_price=entry, stop_price=stop, target_price=None,
            max_hold_days=mh, atr_at_signal=float(atr), notes={}))
    return sigs


def dsr_from_curve(res):
    ec = getattr(res, "equity_curve", None)
    if ec is None or len(ec) < 30: return float("nan")
    r = pd.Series(ec).pct_change().dropna()
    m = ss.sharpe_moments(list(r.values))
    out = ss.deflated_sharpe(m["sr"], int(m["n"]), m["skew"], m["kurt_raw"],
                             var_trials=ss.annualized_to_per_period(0.5)**2, n_trials=8)
    return float(out["dsr"])


def main():
    tickers = resolve_universe_tickers(SPEC); bench = SPEC["universe"]["benchmark"]
    if bench not in tickers: tickers.append(bench)
    print(f"loading {len(tickers)} tickers ...", flush=True)
    dfs = _load_universe(tickers, START, END, force_refetch=False)
    signals = []
    for t, df in dfs.items(): signals.extend(nr7_replay(df, t, PARAMS))
    print(f"{len(signals)} NR7 breakout signals", flush=True)
    LIVE = dict(fill_model=psim.FILL_MARKETABLE_LIMIT, momentum_buffer=0.03, full_spread_marketable=True)
    wf = psim.simulate_walk_forward(signals, dfs, start=START, end=END, is_years=3, oos_years=1, step_years=1,
        sharpe_min=GATE["sharpe_min"], max_dd_pct=GATE["max_dd_pct"], n_min=GATE["n_min"],
        min_window_sharpe=GATE["min_window_sharpe"], min_window_pass_rate=GATE["min_window_pass_rate"], **LIVE)
    a = wf.aggregate; d = dsr_from_curve(a)
    win = " ".join(f"{sp.in_sample_end.year}:{r.sharpe_annualised:.2f}"
                   f"{'v' if r.sharpe_annualised>0.5 else 'x'}" for sp, r in wf.window_results)
    gate_pass = bool(wf.aggregate_gate_passed and wf.window_clause_passed and d > 0.95)
    lines = ["# Strategy #3 — NR7 contraction breakout — net-of-cost walk-forward gate (pilot, 2026-08-04)", "",
             f"Universe sp500_leaning_88 (+SPY). {START}..{END}. Params {PARAMS}.", "",
             f"- Signals: **{len(signals)}**",
             f"- Aggregate OOS: Sharpe **{a.sharpe_annualised:.2f}** · |MDD| {abs(a.max_drawdown_pct):.1f}% · n {a.n_trades} · fill {a.fill_rate*100:.0f}%",
             f"- Aggregate gate: **{'PASS' if wf.aggregate_gate_passed else 'FAIL'}** · Per-window {wf.n_windows_above_floor}/{wf.n_windows} -> **{'PASS' if wf.window_clause_passed else 'FAIL'}** · DSR **{d:.2f}**",
             f"  - windows: {win}", "",
             f"## Verdict: **{'DEPLOY-CANDIDATE (clears gate)' if gate_pass else 'FAIL — does not clear the net-of-cost gate'}**",
             "- Suggest-only; nothing applied."]
    out = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-04-nr7-contraction-breakout-backtest.md")
    with open(out, "w", encoding="utf-8") as fh: fh.write("\n".join(lines) + "\n")
    print(f"AGG OOS Sharpe={a.sharpe_annualised:.2f} |MDD|={abs(a.max_drawdown_pct):.1f}% n={a.n_trades} "
          f"per-window {wf.n_windows_above_floor}/{wf.n_windows} DSR={d:.2f} -> {'PASS' if gate_pass else 'FAIL'}")
    print("windows:", win); print(f"wrote {out}")


if __name__ == "__main__":
    main()
