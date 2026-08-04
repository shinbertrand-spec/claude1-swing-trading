"""Variance Risk Premium (VRP) / delta-hedged-gamma PROXY — research-only, suggest-only.
Claude1 paper-research pilot, 2026-08-04.

The operator asked about options with delta/gamma hedging. Key fact: a CONTINUOUSLY
delta-hedged option position has, to leading order, P&L = integral of
(1/2)*Gamma*S^2*(sigma_realized^2 - sigma_implied^2) dt, which integrates over the life
of the option to the VARIANCE-SWAP payoff:  realized_var - implied_var  (for a long
position; negate for short). So we do NOT need option prices to measure the EDGE of a
delta-hedged vol book to first order — VIX (implied) vs subsequent realized vol IS the
delta-hedged P&L. That lets us characterize the real edge for FREE, honestly, before
scoping a (data + engine + broker heavy) real implementation.

Convention here (SHORT-GAMMA / short-vol carry, the classic premium-harvest):
  monthly P&L proxy (variance points) = implied_var - realized_var
     where implied_var = (VIX_at_month_start/100)^2, realized_var = var of that month's
     daily log returns, annualized. Positive on average = the variance risk premium.
  LONG-GAMMA (gamma scalping, the "volatile market" play) = the exact negative.

Non-overlapping MONTHLY sampling (one obs / month) for honest, ~un-autocorrelated stats.
This is an IDEALIZED UPPER BOUND on the short-vol edge: it ignores option bid/ask (wide),
discrete-hedging error, vega/skew risk, and margin — ALL of which make a real short-vol
book WORSE. So if the carry looks unattractive here, it is strictly worse live.

Usage:  uv run python ledgers/improvements/2026-08-04-variance-risk-premium-probe.py
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

START, END = "2017-01-01", "2026-05-25"


def _load_close(ticker: str) -> pd.Series:
    df = fetch_ohlcv(ticker, period="10y", interval="1d").df
    cols = {c.lower(): c for c in df.columns}
    c = df[cols["close"]].astype(float)
    ix = pd.to_datetime(df.index)
    if getattr(ix, "tz", None) is not None:
        ix = ix.tz_localize(None)      # normalize tz so SPY / VIX indices align
    c.index = ix.normalize()
    return c.sort_index().loc[START:END]


def main():
    print("fetching SPY + ^VIX daily ...", flush=True)
    spy = _load_close("SPY")
    vix = _load_close("^VIX")
    idx = spy.index.intersection(vix.index)
    print(f"  spy={len(spy)} vix={len(vix)} intersection={len(idx)}", flush=True)
    spy, vix = spy.loc[idx], vix.loc[idx]
    logret = np.log(spy / spy.shift(1))

    # group by calendar month (non-overlapping)
    cal = pd.DataFrame({"logret": logret, "vix": vix})
    cal["ym"] = cal.index.to_period("M")
    rows = []
    for period, grp in cal.groupby("ym"):
        days = grp.index
        if len(days) < 15:
            continue
        implied = float(grp["vix"].iloc[0]) / 100.0               # 1-mo implied at month start
        realized = float(grp["logret"].dropna().std(ddof=1) * math.sqrt(252))   # annualized realized over the month
        iv2, rv2 = implied ** 2, realized ** 2
        rows.append({"month": str(period), "implied": implied, "realized": realized,
                     "short_gamma_pnl": iv2 - rv2})                 # variance points (short vol)
    df = pd.DataFrame(rows)
    s = df["short_gamma_pnl"]
    n = len(s)
    mean_v = float(s.mean()); std_v = float(s.std(ddof=1))
    sharpe = mean_v / std_v * math.sqrt(12) if std_v > 0 else 0.0
    skew = float(((s - mean_v) ** 3).mean() / std_v ** 3) if std_v > 0 else 0.0
    hit = float((s > 0).mean() * 100)
    # equity curve of the short-vol carry (unit variance-notional / month)
    eq = (1 + s).cumprod(); mdd = float(((eq / eq.cummax()) - 1).min() * 100)
    vrp_volpts = float((df["implied"] - df["realized"]).mean() * 100)   # avg premium in vol points
    worst = df.reindex(s.sort_values().index).head(6)[["month", "implied", "realized", "short_gamma_pnl"]]
    best = df.reindex(s.sort_values(ascending=False).index).head(3)[["month", "implied", "realized", "short_gamma_pnl"]]

    lines = ["# Variance Risk Premium / delta-hedged gamma — PROXY (pilot, 2026-08-04)", "",
             "**Research-only. Idealized UPPER BOUND on a delta-hedged SHORT-vol book (ignores option "
             "bid/ask, discrete-hedge error, vega/skew, margin — all adverse). A real book is strictly worse.**", "",
             f"- Non-overlapping monthly obs: **n={n}** ({df['month'].iloc[0]}..{df['month'].iloc[-1]})",
             f"- Avg variance risk premium: **{vrp_volpts:+.1f} vol points** (implied − realized; positive = short-vol earns carry)",
             f"- SHORT-GAMMA carry: monthly mean {mean_v:+.4f} var-pts · **annualized Sharpe {sharpe:.2f}** · hit {hit:.0f}% · "
             f"**skew {skew:.2f}** · |MDD| {abs(mdd):.0f}%",
             f"- LONG-GAMMA (gamma scalp, the 'volatile market' play) = the exact mirror: Sharpe {-sharpe:.2f}, "
             f"POSITIVE skew {-skew:.2f}, pays the carry, wins the tail.", "",
             "### Worst 6 months for SHORT gamma (the crash tail you are selling insurance against)",
             "| month | implied | realized | short-γ P&L (var-pts) |", "|---|---|---|---|"]
    for _, r in worst.iterrows():
        lines.append(f"| {r['month']} | {r['implied']*100:.0f} | {r['realized']*100:.0f} | {r['short_gamma_pnl']:+.4f} |")
    lines += ["", "### Best 3 months for SHORT gamma (calm = collect premium)",
              "| month | implied | realized | short-γ P&L (var-pts) |", "|---|---|---|---|"]
    for _, r in best.iterrows():
        lines.append(f"| {r['month']} | {r['implied']*100:.0f} | {r['realized']*100:.0f} | {r['short_gamma_pnl']:+.4f} |")
    lines += ["", "## Read",
              f"- The premium is REAL: implied runs ~{vrp_volpts:.1f} vol points over realized on average, "
              f"short-vol hit rate {hit:.0f}%. This is the well-documented variance risk premium.",
              f"- But it is a NEGATIVE-SKEW (skew {skew:.2f}) insurance-selling carry: a handful of crash months "
              "(see table) erase many months of premium. Sharpe here is the IDEALIZED ceiling; real option "
              "spreads + hedge error push it materially lower.",
              "- LONG gamma is the mirror — the honest 'profit from a volatile market' expression — but it PAYS "
              "the premium every calm month and only wins when realized blows past implied.",
              "- Neither is expressible in the current equity-only, long-only stack. See the companion scope note.",
              "- Suggest-only; nothing applied."]
    out = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-04-variance-risk-premium-probe.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nn={n}  VRP={vrp_volpts:+.1f} vol-pts  short-gamma Sharpe={sharpe:.2f}  skew={skew:.2f}  hit={hit:.0f}%  |MDD|={abs(mdd):.0f}%")
    print("worst months for short gamma:")
    print(worst.to_string(index=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
