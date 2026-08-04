"""Futures TSMOM long/short — REAL-DATA runner (reads roll-adjusted continuous contracts).
Claude1 paper-research pilot, 2026-08-04. Suggest-only research.

This is the ready-to-run backtest for the real-futures pull (see companion spec
`2026-08-04-futures-real-data-spec.md`). It reads back-adjusted continuous daily settles
from a local directory and runs the same net-of-cost gate as the ETF-proxy first pass —
but on real futures, so roll yield / carry is embedded and shorts are genuine.

DATA CONTRACT (what the operator's Databento pull must produce):
  A directory (default: ledgers/improvements/_futures_data/) with ONE CSV per contract
  root, named <ROOT>.csv, each with at least:
     date,close            # date=YYYY-MM-DD; close=BACK-ADJUSTED continuous settle
  The close series MUST be ratio- or difference-back-adjusted across rolls (no artificial
  gaps). The spec's pull snippet produces this. If the directory is absent/empty this
  script prints instructions and exits 0 (so it is safe to run before the pull).

Method (proper CTA, upgrades over the proxy): inverse-vol risk weighting, per-SECTOR gross
cap, portfolio vol-targeted to TARGET_VOL, 12-1 TSMOM sign (+ a 3-6-12mo blend variant),
monthly rebalance, net of turnover cost. Modes L/S vs LONG-only vs (optional) SPY.
Gate: Sharpe>1.0 & |MDD|<25% & >=50% of 6 OOS yrs (2020-25) clear Sharpe>0.5 & DSR>0.95.

Usage:  uv run python ledgers/improvements/2026-08-04-futures-real-data-backtest.py
"""
from __future__ import annotations
import os, sys, math, glob
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

import numpy as np
import pandas as pd
from tools.backtest import sharpe_stats as ss

DATA_DIR = os.path.join(_ROOT, "ledgers", "improvements", "_futures_data")
START, END = "2010-06-01", "2026-05-25"
OOS_YEARS = [2020, 2021, 2022, 2023, 2024, 2025]
LOOKBACK, SKIP, VOLWIN = 252, 21, 60
TARGET_VOL = 0.12          # annualized portfolio vol target (CTA-typical)
SECTOR_CAP = 0.35          # max gross weight per sector
COST_BPS = 2.0             # per unit turnover (futures are cheap)
GATE = dict(sharpe_min=1.0, mdd_max=25.0, win_sharpe=0.5, win_rate=0.5, dsr_min=0.95)

# root -> sector (see spec for full names/exchanges). Extend freely.
SECTOR = {
    "ES": "equity", "NQ": "equity", "RTY": "equity", "YM": "equity",
    "ZT": "rates", "ZF": "rates", "ZN": "rates", "ZB": "rates", "UB": "rates",
    "6E": "fx", "6J": "fx", "6B": "fx", "6A": "fx", "6C": "fx", "6S": "fx",
    "CL": "energy", "NG": "energy", "HO": "energy", "RB": "energy",
    "GC": "metals", "SI": "metals", "HG": "metals",
    "ZC": "grains", "ZW": "grains", "ZS": "grains", "ZL": "grains", "ZM": "grains", "LE": "grains",
}


def _instructions():
    print(f"NO futures data found in {DATA_DIR}\n")
    print("Run the Databento pull in the companion spec "
          "(2026-08-04-futures-real-data-spec.md) first. Expected: one <ROOT>.csv per\n"
          "contract with columns date,close (back-adjusted continuous settle). Then re-run this.\n")
    print(f"Roots expected: {sorted(SECTOR)}")


def load_prices():
    files = glob.glob(os.path.join(DATA_DIR, "*.csv"))
    if not files:
        return None
    cols = {}
    for f in files:
        root = os.path.splitext(os.path.basename(f))[0]
        try:
            df = pd.read_csv(f)
            cmap = {c.lower(): c for c in df.columns}
            dt = pd.to_datetime(df[cmap["date"]]).dt.normalize()
            close = df[cmap.get("close", cmap.get("settle"))].astype(float)
            s = pd.Series(close.values, index=dt).sort_index()
            cols[root] = s
        except Exception as e:
            print(f"  {root}: read failed — {e!r}", flush=True)
    if not cols:
        return None
    px = pd.DataFrame(cols).sort_index().loc[START:END]
    return px.ffill(limit=5)


def signal_frame(px, mode, blend):
    rets = px.pct_change()
    if blend:
        sig = np.sign(px.shift(SKIP) / px.shift(63) - 1.0) \
            + np.sign(px.shift(SKIP) / px.shift(126) - 1.0) \
            + np.sign(px.shift(SKIP) / px.shift(LOOKBACK) - 1.0)
        sign = np.sign(sig)
    else:
        sign = np.sign(px.shift(SKIP) / px.shift(LOOKBACK) - 1.0)
    if mode == "long":
        sign = sign.clip(lower=0.0)
    return rets, sign


def build_returns(px, mode="ls", blend=False):
    rets, sign = signal_frame(px, mode, blend)
    vol = rets.rolling(VOLWIN).std()
    invvol = 1.0 / vol.replace(0.0, np.nan)
    raw = sign * invvol
    idx = px.index
    month_key = pd.Series(idx, index=idx).dt.to_period("M")
    is_mstart = month_key != month_key.shift(1)
    sectors = pd.Series({c: SECTOR.get(c, "other") for c in px.columns})
    pos = pd.DataFrame(0.0, index=idx, columns=px.columns)
    last = None
    for d in idx:
        if is_mstart.loc[d]:
            row = raw.loc[d].where(px.loc[d].notna(), 0.0).fillna(0.0)
            gross = row.abs().sum()
            row = row / gross if gross > 0 else row
            # per-sector gross cap
            for sec in sectors.unique():
                cols_s = sectors[sectors == sec].index
                g = row[cols_s].abs().sum()
                if g > SECTOR_CAP and g > 0:
                    row[cols_s] = row[cols_s] * (SECTOR_CAP / g)
            last = row
        pos.loc[d] = last if last is not None else 0.0
    port = (pos.shift(1) * rets).sum(axis=1)
    turn = (pos - pos.shift(1)).abs().sum(axis=1)
    port = (port - turn * (COST_BPS / 1e4)).fillna(0.0)
    # vol-target the whole series to TARGET_VOL (scale by trailing realized vol; no lookahead)
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
               if len(r[(r.index >= f"{y}-01-01") & (r.index <= f"{y}-12-31")]) > 20 else float("nan"))
           for y in OOS_YEARS}
    nclear = sum(1 for y in OOS_YEARS if per[y] > GATE["win_sharpe"])
    d = dsr(r, n_trials, var_trials)
    gate = full > GATE["sharpe_min"] and abs(mdd) < GATE["mdd_max"] and nclear / 6 >= GATE["win_rate"] and d > GATE["dsr_min"]
    return dict(sharpe=full, mdd=mdd, cum=cum, per=per, nclear=nclear, dsr=d, gate=gate, n=len(r))


def main():
    px = load_prices()
    if px is None or px.shape[1] == 0:
        _instructions(); return
    print(f"loaded {px.shape[1]} contracts, {px.index.min().date()}..{px.index.max().date()}", flush=True)
    variants = {
        "L/S 12-1": build_returns(px, "ls", blend=False),
        "L/S 3-6-12 blend": build_returns(px, "ls", blend=True),
        "LONG-only 12-1": build_returns(px, "long", blend=False),
    }
    grosses = [ann_sharpe(v) for v in variants.values()]
    var_trials = float(np.var([ss.annualized_to_per_period(x) for x in grosses], ddof=1)) if len(grosses) > 1 else 1e-6
    n_trials = 6
    R = {k: evaluate(v, n_trials, var_trials) for k, v in variants.items()}
    spy_corr = ""
    if "ES" in px.columns:
        es_ret = px["ES"].pct_change().fillna(0.0)
        c = float(pd.concat([variants["L/S 12-1"], es_ret], axis=1).dropna().corr().iloc[0, 1])
        spy_corr = f"{c:+.2f}"

    lines = ["# Futures TSMOM long/short — REAL DATA (roll-adjusted continuous) — net-of-cost gate (pilot, 2026-08-04)", "",
             f"{px.shape[1]} contracts across {sorted(set(SECTOR.get(c,'other') for c in px.columns))}. "
             f"{px.index.min().date()}..{px.index.max().date()}. 12-1 (+3-6-12 blend) TSMOM, inverse-vol risk weight, "
             f"{SECTOR_CAP:.0%} sector cap, {TARGET_VOL:.0%} vol target, {COST_BPS:.0f}bps turnover cost.", "",
             "| variant | Sharpe | |MDD|% | cum% | OOS yrs>0.5 | DSR | GATE |", "|---|---|---|---|---|---|---|"]
    for k, r in R.items():
        lines.append(f"| {k} | {r['sharpe']:.2f} | {abs(r['mdd']):.1f} | {r['cum']*100:.0f} | {r['nclear']}/6 | {r['dsr']:.2f} | {'PASS' if r['gate'] else 'FAIL'} |")
    if spy_corr:
        lines += ["", f"- L/S ↔ ES(S&P) correlation: **{spy_corr}** (diversification vs our equity-long book)"]
    lines += ["", "### Per-OOS-year Sharpe", "| year | " + " | ".join(R) + " |", "|" + "---|" * (len(R) + 1)]
    for y in OOS_YEARS:
        lines.append(f"| {y} | " + " | ".join(f"{R[k]['per'][y]:.2f}" for k in R) + " |")
    anypass = any(r["gate"] for r in R.values())
    lines += ["", f"## Verdict: **{'a REAL-DATA variant clears the gate -> take to blind judge+critic' if anypass else 'no variant clears the gate on real futures'}**",
              "- Promote criterion: gate PASS AND diversifying (|corr| to ES < ~0.3, positive in 2022/crisis years).",
              "- Kill criterion: real-data L/S no better than the ETF proxy (Sharpe < ~0.5) -> edge confirmed absent, stop.",
              "- Suggest-only; nothing applied."]
    out = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-04-futures-real-data-backtest.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    for k, r in R.items():
        print(f"{k:20} Sharpe={r['sharpe']:.2f} |MDD|={abs(r['mdd']):.1f}% OOS {r['nclear']}/6 DSR={r['dsr']:.2f} -> {'PASS' if r['gate'] else 'FAIL'}")
    print(f"corr(L/S,ES)={spy_corr}  wrote {out}")


if __name__ == "__main__":
    main()
