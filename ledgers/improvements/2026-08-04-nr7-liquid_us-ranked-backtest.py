"""Strategy #3b — NR7 CONTRACTION BREAKOUT, broadened to liquid_us + RANKED.
Claude1 paper-research pilot, 2026-08-04. Suggest-only research.

Follow-up to the 88-name NR7 marginal pass (Sharpe 1.00 / MDD 11% / 4-of-6 windows).
Broadening naively to the ~1178-name liquid_us universe would drown the 8-position cap
in signals (the dual_ma / connors cap-artifact failure). So we RANK: on each signal
date, among all NR7 breakouts firing, keep the top-8 by trailing 63-day return
(trend-confirmed contraction) — the same "add a top-K rank" fix that rescued ts_momentum.

Same net-of-cost walk-forward gate (marketable-limit + full spread + OHLC + 8-cap;
rolling 3y-IS/1y-OOS/1y-step). Gate: agg Sharpe>1.0 & |MDD|<25% & n>=30 AND >=50% OOS
windows clear Sharpe>0.5, plus Deflated Sharpe>0.95. NET OF COST.

Usage:  uv run python ledgers/improvements/2026-08-04-nr7-liquid_us-ranked-backtest.py
"""
from __future__ import annotations
import os, sys
from collections import defaultdict
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

FILL_KIND = "ts_momentum"
SPEC = {"universe": {"name": "liquid_us_2026q2", "benchmark": "SPY"}}
START, END = date(2017, 1, 1), date(2026, 5, 25)
PARAMS = dict(nr_window=7, atr_period=20, atr_stop_multiple=2.5, max_hold_days=20,
              cooldown_days=5, rank_lookback=63, top_k=8)
GATE = dict(sharpe_min=1.0, max_dd_pct=25.0, n_min=30, min_window_sharpe=0.5, min_window_pass_rate=0.5)


def _atr(df, i, period):
    if i < period: return None
    h = df["High"].values; l = df["Low"].values; c = df["Close"].values
    trs = [max(h[k]-l[k], abs(h[k]-c[k-1]), abs(l[k]-c[k-1])) for k in range(i-period+1, i+1)]
    return float(np.mean(trs)) if trs else None


def nr7_signals(df, ticker, params):
    if ticker == SPEC["universe"]["benchmark"]: return []
    nw = int(params["nr_window"]); atrp = int(params["atr_period"]); mult = float(params["atr_stop_multiple"])
    mh = int(params["max_hold_days"]); cd = int(params["cooldown_days"]); rl = int(params["rank_lookback"])
    high = df["High"].values; low = df["Low"].values; close = df["Close"].values; rng = high - low
    idx = list(df.index); out = []; last = -10**9
    for i in range(max(nw, atrp, rl), len(df)-1):
        if rng[i] != rng[i-nw+1:i+1].min(): continue
        nxt = df.iloc[i+1]
        if not (float(nxt["High"]) > high[i]): continue
        if i - last < cd: continue
        atr = _atr(df, i, atrp)
        if atr is None or atr <= 0: continue
        entry = max(float(nxt["Open"]), float(high[i]))
        stop = entry - mult*atr
        if stop >= entry or entry <= 0: continue
        score = (close[i] / close[i-rl]) - 1.0        # trailing 63d return = trend confirmation
        last = i
        out.append((pd.Timestamp(idx[i]).date(), score, TradeSignal(
            ticker=ticker, setup_type=FILL_KIND, setup_grade="B",
            entry_date=pd.Timestamp(idx[i]).date(), fill_date=pd.Timestamp(idx[i+1]).date(),
            entry_price=entry, stop_price=stop, target_price=None,
            max_hold_days=mh, atr_at_signal=float(atr), notes={})))
    return out


def dsr_from_curve(res, n_trials):
    ec = getattr(res, "equity_curve", None)
    if ec is None or len(ec) < 30: return float("nan")
    r = pd.Series(ec).pct_change().dropna()
    m = ss.sharpe_moments(list(r.values))
    out = ss.deflated_sharpe(m["sr"], int(m["n"]), m["skew"], m["kurt_raw"],
                             var_trials=ss.annualized_to_per_period(0.5)**2, n_trials=n_trials)
    return float(out["dsr"])


def main():
    tickers = resolve_universe_tickers(SPEC); bench = SPEC["universe"]["benchmark"]
    if bench not in tickers: tickers.append(bench)
    print(f"loading {len(tickers)} tickers ...", flush=True)
    dfs = _load_universe(tickers, START, END, force_refetch=False)
    raw = []
    for t, df in dfs.items(): raw.extend(nr7_signals(df, t, PARAMS))
    # rank: keep top_k by trailing-return per signal date
    by_date = defaultdict(list)
    for d, sc, sig in raw: by_date[d].append((sc, sig))
    K = int(PARAMS["top_k"]); signals = []
    for d, lst in by_date.items():
        lst.sort(key=lambda x: x[0], reverse=True)
        signals.extend(sig for _, sig in lst[:K])
    print(f"{len(raw)} raw NR7 signals -> {len(signals)} after top-{K}/day rank", flush=True)
    LIVE = dict(fill_model=psim.FILL_MARKETABLE_LIMIT, momentum_buffer=0.03, full_spread_marketable=True)
    wf = psim.simulate_walk_forward(signals, dfs, start=START, end=END, is_years=3, oos_years=1, step_years=1,
        sharpe_min=GATE["sharpe_min"], max_dd_pct=GATE["max_dd_pct"], n_min=GATE["n_min"],
        min_window_sharpe=GATE["min_window_sharpe"], min_window_pass_rate=GATE["min_window_pass_rate"], **LIVE)
    a = wf.aggregate; d = dsr_from_curve(a, n_trials=10)   # 10 = strategy-variants explored in the pilot
    win = " ".join(f"{sp.in_sample_end.year}:{r.sharpe_annualised:.2f}"
                   f"{'v' if r.sharpe_annualised>0.5 else 'x'}" for sp, r in wf.window_results)
    gate_pass = bool(wf.aggregate_gate_passed and wf.window_clause_passed and d > 0.95)
    lines = ["# Strategy #3b — NR7 contraction breakout (liquid_us + top-8 rank) — net-of-cost gate (pilot, 2026-08-04)", "",
             f"Universe liquid_us_2026q2 (~1178 +SPY). {START}..{END}. Params {PARAMS}.",
             "Ranked: top-8 per signal date by trailing 63d return (trend-confirmed contraction).", "",
             f"- Raw signals -> ranked: see stdout. Aggregate OOS: Sharpe **{a.sharpe_annualised:.2f}** · |MDD| {abs(a.max_drawdown_pct):.1f}% · n {a.n_trades} · fill {a.fill_rate*100:.0f}%",
             f"- Aggregate gate **{'PASS' if wf.aggregate_gate_passed else 'FAIL'}** · Per-window {wf.n_windows_above_floor}/{wf.n_windows} -> **{'PASS' if wf.window_clause_passed else 'FAIL'}** · DSR **{d:.2f}**",
             f"  - windows: {win}", "",
             f"## Verdict: **{'DEPLOY-CANDIDATE (clears gate) -> take to blind judge+critic' if gate_pass else 'FAIL — does not clear the net-of-cost gate'}**",
             "- Suggest-only; nothing applied."]
    out = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-04-nr7-liquid_us-ranked-backtest.md")
    with open(out, "w", encoding="utf-8") as fh: fh.write("\n".join(lines) + "\n")
    print(f"AGG OOS Sharpe={a.sharpe_annualised:.2f} |MDD|={abs(a.max_drawdown_pct):.1f}% n={a.n_trades} "
          f"per-window {wf.n_windows_above_floor}/{wf.n_windows} DSR={d:.2f} -> {'PASS' if gate_pass else 'FAIL'}")
    print("windows:", win); print(f"wrote {out}")


if __name__ == "__main__":
    main()
