"""Futures TIME-SERIES MOMENTUM, long/short, diversified — research backtest, suggest-only.
Claude1 paper-research pilot, 2026-08-04.

The open research item (memory `project_crossasset_trend`): a CLEAN long/short trend
backtest before any futures infra/doctrine change. This runs it on FREE futures-proxy
ETFs across 5 asset classes. Shorts are simulated by shorting the CASH ETF directly (NOT
inverse ETFs) — so this AVOIDS the inverse-ETF decay confound that killed the prior
synthetic-short attempt. Remaining caveat: ETFs are not futures (roll yield + financing
differ); a real test needs roll-adjusted continuous contracts (~$50-300 data pull).

Classic Moskowitz-Ooi-Pedersen TSMOM: monthly rebalance; position sign = sign of the
12-1 momentum (return from t-252 to t-21); inverse-realized-vol risk weighting across
instruments (gross exposure = 1). Net of turnover cost. Compared head-to-head:
  L/S    : sign in {+1,-1}          (the new thing — adds shorts)
  LONG   : sign in {+1, 0}          (long-only, flat when trend down — our current shape)
  SPY B&H: buy-and-hold benchmark

Gate/characterize on the daily return series (same net-of-cost gate family):
Sharpe>1.0 & |MDD|<25% & >=50% of 6 OOS yrs (2020-25) clear Sharpe>0.5 & Deflated Sharpe>0.95.

Usage:  uv run python ledgers/improvements/2026-08-04-futures-tsmom-longshort-backtest.py
"""
from __future__ import annotations
import os, sys, math
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

import numpy as np
import pandas as pd
from tools.data import fetch_ohlcv
from tools.backtest import sharpe_stats as ss

# futures-proxy ETFs across 5 asset classes
UNIVERSE = {
    "equity":   ["SPY", "QQQ", "IWM", "EFA", "EEM"],
    "rates":    ["TLT", "IEF"],
    "commod":   ["GLD", "SLV", "USO", "DBC", "DBA"],
    "fx":       ["UUP", "FXE", "FXY"],
    "reit":     ["VNQ"],
}
TICKERS = [t for v in UNIVERSE.values() for t in v]
START, END = "2010-01-01", "2026-05-25"
OOS_YEARS = [2020, 2021, 2022, 2023, 2024, 2025]
LOOKBACK, SKIP, VOLWIN = 252, 21, 60
COST_BPS = 3.0     # per unit turnover, round-trip-ish on liquid ETFs
GATE = dict(sharpe_min=1.0, mdd_max=25.0, win_sharpe=0.5, win_rate=0.5, dsr_min=0.95)


def load_prices() -> pd.DataFrame:
    cols = {}
    for t in TICKERS:
        try:
            df = fetch_ohlcv(t, period="20y", interval="1d").df
            cmap = {c.lower(): c for c in df.columns}
            s = df[cmap["close"]].astype(float)
            ix = pd.to_datetime(df.index)
            if getattr(ix, "tz", None) is not None:
                ix = ix.tz_localize(None)
            s.index = ix.normalize()
            cols[t] = s.sort_index()
        except Exception as e:
            print(f"  {t}: fetch failed — {e!r}", flush=True)
    px = pd.DataFrame(cols).sort_index().loc[START:END]
    return px.ffill(limit=5)


def build_returns(px, mode):
    """mode in {'ls','long'}; returns daily portfolio return series net of cost."""
    rets = px.pct_change()
    mom = px.shift(SKIP) / px.shift(LOOKBACK) - 1.0        # 12-1 momentum, known at t
    vol = rets.rolling(VOLWIN).std()
    sign = np.sign(mom)
    if mode == "long":
        sign = sign.clip(lower=0.0)                        # long-only: flat when trend down
    invvol = 1.0 / vol.replace(0.0, np.nan)
    raw = sign * invvol
    # month-start rebalance dates
    idx = px.index
    month_key = pd.Series(idx, index=idx).dt.to_period("M")
    is_mstart = month_key != month_key.shift(1)
    pos = pd.DataFrame(0.0, index=idx, columns=px.columns)
    last = None
    for d in idx:
        if is_mstart.loc[d]:
            row = raw.loc[d].copy()
            row = row.where(px.loc[d].notna(), 0.0).fillna(0.0)
            gross = row.abs().sum()
            row = row / gross if gross > 0 else row
            last = row
        pos.loc[d] = last if last is not None else 0.0
    port = (pos.shift(1) * rets).sum(axis=1)
    # turnover cost on rebalance days
    turn = (pos - pos.shift(1)).abs().sum(axis=1)
    port = port - turn * (COST_BPS / 1e4)
    return port.fillna(0.0)


def ann_sharpe(r):
    r = pd.Series(r)
    return float(r.mean() / r.std(ddof=1) * math.sqrt(252)) if r.std(ddof=1) > 0 and len(r) > 2 else 0.0

def max_dd(r):
    eq = (1 + pd.Series(r)).cumprod(); return float(((eq / eq.cummax()) - 1).min() * 100)

def dsr(r, n_trials, var_trials):
    m = ss.sharpe_moments(list(pd.Series(r).values))
    return float(ss.deflated_sharpe(m["sr"], int(m["n"]), m["skew"], m["kurt_raw"],
                                    var_trials=max(var_trials, 1e-12), n_trials=n_trials)["dsr"])


def evaluate(r, n_trials, var_trials, oos_only=True):
    full = ann_sharpe(r); mdd = max_dd(r); cum = float((1 + r).prod() - 1)
    per = {}
    for y in OOS_YEARS:
        ry = r[(r.index >= f"{y}-01-01") & (r.index <= f"{y}-12-31")]
        per[y] = ann_sharpe(ry) if len(ry) > 20 else float("nan")
    nclear = sum(1 for y in OOS_YEARS if per[y] > GATE["win_sharpe"])
    d = dsr(r, n_trials, var_trials)
    gate = full > GATE["sharpe_min"] and abs(mdd) < GATE["mdd_max"] and nclear / 6 >= GATE["win_rate"] and d > GATE["dsr_min"]
    return dict(sharpe=full, mdd=mdd, cum=cum, per=per, nclear=nclear, dsr=d, gate=gate, n=len(r))


def main():
    print(f"loading {len(TICKERS)} futures-proxy ETFs ...", flush=True)
    px = load_prices()
    print(f"price matrix {px.shape}, {px.index.min().date()}..{px.index.max().date()}", flush=True)
    ls = build_returns(px, "ls")
    lo = build_returns(px, "long")
    spy = px["SPY"].pct_change().fillna(0.0)
    n_trials = 6
    grosses = [ann_sharpe(ls), ann_sharpe(lo)]
    var_trials = float(np.var([ss.annualized_to_per_period(x) for x in grosses], ddof=1)) if len(grosses) > 1 else 1e-6
    R = {"L/S TSMOM": evaluate(ls, n_trials, var_trials),
         "LONG-only TSMOM": evaluate(lo, n_trials, var_trials),
         "SPY buy&hold": evaluate(spy, n_trials, var_trials)}
    corr_ls_spy = float(pd.concat([ls, spy], axis=1).dropna().corr().iloc[0, 1])

    lines = ["# Futures TSMOM long/short (proxy ETFs) — net-of-cost gate (pilot, 2026-08-04)", "",
             f"Universe {TICKERS} across {list(UNIVERSE)}. {START}..{END}. 12-1 TSMOM sign, inverse-vol risk "
             f"weight, monthly rebalance, {COST_BPS:.0f}bps turnover cost. Shorts = short the cash ETF (no "
             "inverse-ETF decay). ETF≠futures caveat (roll/financing).", "",
             "| strategy | Sharpe | |MDD|% | cum% | OOS yrs>0.5 | DSR | GATE |", "|---|---|---|---|---|---|---|"]
    for name, r in R.items():
        lines.append(f"| {name} | {r['sharpe']:.2f} | {abs(r['mdd']):.1f} | {r['cum']*100:.0f} | "
                     f"{r['nclear']}/6 | {r['dsr']:.2f} | {'PASS' if r['gate'] else 'FAIL'} |")
    lines += ["", f"- L/S ↔ SPY correlation: **{corr_ls_spy:+.2f}** (diversification vs our equity-long book)",
              "", "### Per-OOS-year Sharpe", "| year | L/S | LONG | SPY |", "|---|---|---|---|"]
    for y in OOS_YEARS:
        lines.append(f"| {y} | {R['L/S TSMOM']['per'][y]:.2f} | {R['LONG-only TSMOM']['per'][y]:.2f} | {R['SPY buy&hold']['per'][y]:.2f} |")
    anypass = any(R[k]["gate"] for k in ["L/S TSMOM", "LONG-only TSMOM"])
    lines += ["", f"## Verdict: **{'a TSMOM variant clears the gate' if anypass else 'no TSMOM variant clears the gate'}**",
              "- Key question was diversification (low/neg corr to our equity-long edge), not just standalone Sharpe.",
              "- Real futures (roll-adjusted continuous contracts) needed to confirm — this is the free ETF-proxy first pass.",
              "- Suggest-only; nothing applied."]
    out = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-04-futures-tsmom-longshort-backtest.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\ncorr(L/S, SPY)={corr_ls_spy:+.2f}")
    for name, r in R.items():
        print(f"{name:18} Sharpe={r['sharpe']:.2f} |MDD|={abs(r['mdd']):.1f}% cum={r['cum']*100:.0f}% "
              f"OOS {r['nclear']}/6 DSR={r['dsr']:.2f} -> {'PASS' if r['gate'] else 'FAIL'}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
