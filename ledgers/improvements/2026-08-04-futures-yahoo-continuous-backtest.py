"""Futures TSMOM long/short on YAHOO CONTINUOUS FUTURES (=F) — free real-data pass.
Claude1 paper-research pilot, 2026-08-04. Suggest-only research.

The safe, $0, no-API-key version of the real-futures test: Yahoo's continuous front-month
futures (ES=F, CL=F, GC=F, ...) are REAL futures (not ETF proxies) — a genuine upgrade over
the ETF-proxy first pass. Caveat vs a paid Databento back-adjusted pull: Yahoo's continuous
series is NOT cleanly Panama-back-adjusted, so contract rolls inject spurious daily-return
spikes. We winsorize gross roll artifacts (clip daily returns at +/-CLIP) and REPORT the
clip count so the contamination is visible. This is a free intermediate between the ETF
proxy and the clean Databento pull (spec: 2026-08-04-futures-real-data-spec.md).

Same method as the ETF proxy / real-data runner: 12-1 TSMOM sign (+ 3-6-12 blend), inverse-
vol risk weight, per-sector gross cap, portfolio vol-target, monthly rebalance, net cost.
Gate: Sharpe>1.0 & |MDD|<25% & >=50% of 6 OOS yrs (2020-25) clear Sharpe>0.5 & DSR>0.95.

Usage:  uv run python ledgers/improvements/2026-08-04-futures-yahoo-continuous-backtest.py
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

# Yahoo continuous-futures symbols by sector (=F suffix). Loader tolerates failures.
UNIVERSE = {
    "equity": ["ES=F", "NQ=F", "YM=F", "RTY=F"],
    "rates":  ["ZT=F", "ZF=F", "ZN=F", "ZB=F", "UB=F"],
    "fx":     ["6E=F", "6J=F", "6B=F", "6A=F", "6C=F", "6S=F"],
    "energy": ["CL=F", "NG=F", "HO=F", "RB=F", "BZ=F"],
    "metals": ["GC=F", "SI=F", "HG=F", "PL=F"],
    "grains": ["ZC=F", "ZW=F", "ZS=F", "ZL=F", "ZM=F", "LE=F"],
    "softs":  ["KC=F", "SB=F", "CT=F", "CC=F"],
}
SECTOR = {t: sec for sec, ts in UNIVERSE.items() for t in ts}
TICKERS = list(SECTOR)
START, END = "2007-01-01", "2026-05-25"
OOS_YEARS = [2020, 2021, 2022, 2023, 2024, 2025]
LOOKBACK, SKIP, VOLWIN = 252, 21, 60
TARGET_VOL, SECTOR_CAP, COST_BPS = 0.12, 0.35, 2.0
CLIP = 0.15               # winsorize daily returns to +/-15% to kill gross roll artifacts
GATE = dict(sharpe_min=1.0, mdd_max=25.0, win_sharpe=0.5, win_rate=0.5, dsr_min=0.95)


def load_prices():
    cols = {}
    for t in TICKERS:
        try:
            df = fetch_ohlcv(t, period="25y", interval="1d").df
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


def clipped_returns(px):
    r = px.pct_change()
    n_clipped = int(((r.abs() > CLIP) & r.notna()).sum().sum())
    return r.clip(-CLIP, CLIP), n_clipped


def build_returns(px, rets, mode, blend):
    if blend:
        sign = np.sign(np.sign(px.shift(SKIP) / px.shift(63) - 1.0)
                       + np.sign(px.shift(SKIP) / px.shift(126) - 1.0)
                       + np.sign(px.shift(SKIP) / px.shift(LOOKBACK) - 1.0))
    else:
        sign = np.sign(px.shift(SKIP) / px.shift(LOOKBACK) - 1.0)
    if mode == "long":
        sign = sign.clip(lower=0.0)
    vol = rets.rolling(VOLWIN).std()
    raw = sign * (1.0 / vol.replace(0.0, np.nan))
    idx = px.index
    mk = pd.Series(idx, index=idx).dt.to_period("M")
    is_mstart = mk != mk.shift(1)
    sectors = pd.Series({c: SECTOR.get(c, "other") for c in px.columns})
    pos = pd.DataFrame(0.0, index=idx, columns=px.columns); last = None
    for d in idx:
        if is_mstart.loc[d]:
            row = raw.loc[d].where(px.loc[d].notna(), 0.0).fillna(0.0)
            g = row.abs().sum(); row = row / g if g > 0 else row
            for sec in sectors.unique():
                cs = sectors[sectors == sec].index; gs = row[cs].abs().sum()
                if gs > SECTOR_CAP and gs > 0: row[cs] = row[cs] * (SECTOR_CAP / gs)
            last = row
        pos.loc[d] = last if last is not None else 0.0
    port = (pos.shift(1) * rets).sum(axis=1)
    turn = (pos - pos.shift(1)).abs().sum(axis=1)
    port = (port - turn * (COST_BPS / 1e4)).fillna(0.0)
    rv = port.rolling(VOLWIN).std().shift(1) * math.sqrt(252)
    scale = (TARGET_VOL / rv).clip(upper=5.0).fillna(1.0)
    return (port * scale).fillna(0.0)


def ann_sharpe(r):
    r = pd.Series(r); return float(r.mean() / r.std(ddof=1) * math.sqrt(252)) if r.std(ddof=1) > 0 and len(r) > 2 else 0.0
def max_dd(r):
    eq = (1 + pd.Series(r)).cumprod(); return float(((eq / eq.cummax()) - 1).min() * 100)
def dsr(r, n_trials, var_trials):
    m = ss.sharpe_moments(list(pd.Series(r).values))
    return float(ss.deflated_sharpe(m["sr"], int(m["n"]), m["skew"], m["kurt_raw"], var_trials=max(var_trials, 1e-12), n_trials=n_trials)["dsr"])


def evaluate(r, n_trials, var_trials):
    full = ann_sharpe(r); mdd = max_dd(r); cum = float((1 + r).prod() - 1)
    per = {y: (ann_sharpe(r[(r.index >= f"{y}-01-01") & (r.index <= f"{y}-12-31")])
               if len(r[(r.index >= f"{y}-01-01") & (r.index <= f"{y}-12-31")]) > 20 else float("nan")) for y in OOS_YEARS}
    nclear = sum(1 for y in OOS_YEARS if per[y] > GATE["win_sharpe"])
    d = dsr(r, n_trials, var_trials)
    gate = full > GATE["sharpe_min"] and abs(mdd) < GATE["mdd_max"] and nclear / 6 >= GATE["win_rate"] and d > GATE["dsr_min"]
    return dict(sharpe=full, mdd=mdd, cum=cum, per=per, nclear=nclear, dsr=d, gate=gate, n=len(r))


def main():
    print(f"loading {len(TICKERS)} Yahoo continuous futures ...", flush=True)
    px = load_prices()
    print(f"loaded {px.shape[1]} contracts, {px.index.min().date()}..{px.index.max().date()}", flush=True)
    rets, n_clipped = clipped_returns(px)
    tot = int(rets.notna().sum().sum())
    variants = {
        "L/S 12-1": build_returns(px, rets, "ls", False),
        "L/S 3-6-12 blend": build_returns(px, rets, "ls", True),
        "LONG-only 12-1": build_returns(px, rets, "long", False),
    }
    grosses = [ann_sharpe(v) for v in variants.values()]
    var_trials = float(np.var([ss.annualized_to_per_period(x) for x in grosses], ddof=1)) if len(grosses) > 1 else 1e-6
    R = {k: evaluate(v, 6, var_trials) for k, v in variants.items()}
    corr = ""
    if "ES=F" in px.columns:
        es = rets["ES=F"].fillna(0.0)
        corr = f"{float(pd.concat([variants['L/S 12-1'], es], axis=1).dropna().corr().iloc[0,1]):+.2f}"

    lines = ["# Futures TSMOM long/short — YAHOO CONTINUOUS (free real-futures pass) — net-of-cost gate (pilot, 2026-08-04)", "",
             f"{px.shape[1]} Yahoo =F continuous contracts across {sorted(set(SECTOR.values()))}. "
             f"{px.index.min().date()}..{px.index.max().date()}. 12-1 (+blend) TSMOM, inverse-vol weight, "
             f"{SECTOR_CAP:.0%} sector cap, {TARGET_VOL:.0%} vol target, {COST_BPS:.0f}bps cost. "
             f"**Roll-artifact winsorize: {n_clipped}/{tot} daily returns clipped at ±{CLIP:.0%} ({100*n_clipped/max(tot,1):.2f}%).**", "",
             "| variant | Sharpe | |MDD|% | cum% | OOS yrs>0.5 | DSR | GATE |", "|---|---|---|---|---|---|---|"]
    for k, r in R.items():
        lines.append(f"| {k} | {r['sharpe']:.2f} | {abs(r['mdd']):.1f} | {r['cum']*100:.0f} | {r['nclear']}/6 | {r['dsr']:.2f} | {'PASS' if r['gate'] else 'FAIL'} |")
    if corr:
        lines += ["", f"- L/S ↔ ES(S&P) correlation: **{corr}**"]
    lines += ["", "### Per-OOS-year Sharpe", "| year | " + " | ".join(R) + " |", "|" + "---|" * (len(R) + 1)]
    for y in OOS_YEARS:
        lines.append(f"| {y} | " + " | ".join(f"{R[k]['per'][y]:.2f}" for k in R) + " |")
    anypass = any(r["gate"] for r in R.values())
    lines += ["", f"## Verdict: **{'a variant clears the gate on free real futures' if anypass else 'no variant clears the gate on free real futures'}**",
              "- Free real-futures upgrade over the ETF proxy; roll-gap noise is the residual (winsorized above).",
              "- Confirmatory step if a pulse appears: the clean Databento back-adjusted pull (spec on file).",
              "- Suggest-only; nothing applied."]
    out = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-04-futures-yahoo-continuous-backtest.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nclipped {n_clipped}/{tot} ({100*n_clipped/max(tot,1):.2f}%) daily returns; corr(L/S,ES)={corr}")
    for k, r in R.items():
        print(f"{k:20} Sharpe={r['sharpe']:.2f} |MDD|={abs(r['mdd']):.1f}% cum={r['cum']*100:.0f}% OOS {r['nclear']}/6 DSR={r['dsr']:.2f} -> {'PASS' if r['gate'] else 'FAIL'}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
