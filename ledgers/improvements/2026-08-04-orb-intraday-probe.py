"""ORB (Opening Range Breakout) — SHALLOW INTRADAY PROBE, research-only / NOT deployable.
Claude1 paper-research pilot, 2026-08-04. Suggest-only.

*** THIS IS NOT A GATE-CLEARING BACKTEST. *** yfinance intraday history is shallow
(1h bars ~<=730 days) and hourly bars are too coarse for a real opening-range test.
Purpose: a directional pulse-check on whether an ORB has ANY edge, before deciding
whether to invest in a real intraday data source (Alpaca/Polygon minute bars). Any
number here is over ~2 years of hourly bars and canNOT be walk-forward / DSR gated.

Rule (long-only, coarse 1h ORB): opening range = first hourly bar of the day. If a
later bar's high breaks the OR high, enter long at the OR high; exit at the day's last
bar close; hard stop at the OR low. One trade per name per day. Net of a round-trip cost.

Usage:  uv run python ledgers/improvements/2026-08-04-orb-intraday-probe.py
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

TICKERS = ["SPY", "QQQ", "NVDA", "TSLA", "AMD", "META"]   # liquid + volatile = ORB candidates
COST_BPS = 5.0            # round-trip
STOP = True


def orb_trades(ticker: str) -> pd.DataFrame:
    try:
        df = fetch_ohlcv(ticker, period="730d", interval="1h").df
    except Exception as e:
        print(f"  {ticker}: fetch failed — {e!r}", flush=True); return pd.DataFrame()
    if df is None or len(df) < 50:
        print(f"  {ticker}: too few bars ({0 if df is None else len(df)})", flush=True); return pd.DataFrame()
    cols = {c.lower(): c for c in df.columns}
    O, H, L, C = cols.get("open"), cols.get("high"), cols.get("low"), cols.get("close")
    idx = pd.to_datetime(df.index)
    df = df.assign(_date=idx.date)
    recs = []
    for d, g in df.groupby("_date"):
        if len(g) < 3: continue
        g = g.sort_index()
        or_bar = g.iloc[0]
        or_hi, or_lo = float(or_bar[H]), float(or_bar[L])
        rest = g.iloc[1:]
        entered = False; entry = ret = None
        for _, bar in rest.iterrows():
            if not entered and float(bar[H]) > or_hi:
                entry = or_hi; entered = True
                exit_px = float(g.iloc[-1][C])
                if STOP:
                    # if any subsequent bar low breaches OR low, stop out there
                    after = rest.loc[bar.name:]
                    stopped = after[after[L].astype(float) <= or_lo]
                    if len(stopped): exit_px = or_lo
                ret = (exit_px/entry) - 1.0 - COST_BPS/1e4
                break
        if entered: recs.append({"date": pd.Timestamp(d), "ticker": ticker, "ret": ret})
    return pd.DataFrame(recs)


def main():
    print("fetching ~730d hourly bars (yfinance) ...", flush=True)
    allt = pd.concat([orb_trades(t) for t in TICKERS], ignore_index=True) if TICKERS else pd.DataFrame()
    if allt.empty:
        print("NO intraday data retrieved — yfinance intraday unavailable/too shallow. "
              "Confirms: a real ORB test needs a paid intraday source (Alpaca/Polygon).")
        return
    # equal-weight daily strategy return across names that traded
    daily = allt.groupby("date")["ret"].mean().sort_index()
    n = len(allt); span = f"{allt['date'].min().date()}..{allt['date'].max().date()}"
    sharpe = float(daily.mean()/daily.std(ddof=1)*math.sqrt(252)) if daily.std(ddof=1) > 0 else 0.0
    hit = float((allt["ret"] > 0).mean()); avg = float(allt["ret"].mean())
    eq = (1+daily).cumprod(); mdd = float(((eq/eq.cummax())-1).min()*100)
    per_ticker = allt.groupby("ticker")["ret"].agg(["count", "mean"])
    lines = ["# ORB intraday probe — RESEARCH-ONLY, NOT GATE-CLEARED (pilot, 2026-08-04)", "",
             "**Coarse 1h opening-range breakout over ~2yr of yfinance hourly bars. This CANNOT be "
             "walk-forward / DSR / net-cost gated (data too shallow + hourly too coarse). Directional "
             "pulse-check only, to decide whether a real intraday data source is worth buying.**", "",
             f"- Trades: {n} over {span} · tickers {TICKERS}",
             f"- Per-trade avg return (net {COST_BPS:.0f}bps): {avg*100:.3f}% · hit rate {hit*100:.1f}%",
             f"- Daily-strategy annualized Sharpe (indicative): **{sharpe:.2f}** · |MDD| {abs(mdd):.1f}%", "",
             "| ticker | trades | avg ret % |", "|---|---|---|"]
    for t, row in per_ticker.iterrows():
        lines.append(f"| {t} | {int(row['count'])} | {row['mean']*100:.3f} |")
    verdict = ("shows a pulse — worth a real intraday data source" if (sharpe > 0.8 and avg > 0)
               else "NO usable edge in this shallow probe")
    lines += ["", f"## Indicative read: **{verdict}** (NOT a gate verdict — cannot deploy off this).",
              "- Suggest-only; nothing applied."]
    out = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-04-orb-intraday-probe.md")
    with open(out, "w", encoding="utf-8") as fh: fh.write("\n".join(lines)+"\n")
    print(f"trades={n} avg={avg*100:.3f}% hit={hit*100:.1f}% indicative Sharpe={sharpe:.2f} |MDD|={abs(mdd):.1f}%")
    print(per_ticker); print(f"wrote {out}")


if __name__ == "__main__":
    main()
