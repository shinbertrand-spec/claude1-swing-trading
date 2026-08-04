"""Hourly intraday strategies — VOL-REGIME PROBE, research-only / NOT deployable.
Claude1 paper-research pilot, 2026-08-04. Suggest-only.

*** NOT A GATE-CLEARING BACKTEST. *** yfinance hourly history is ~<=730 days, which
cannot support the project's 6-window rolling walk-forward + Deflated-Sharpe gate over
2017-2026. This is a directional pulse-check on whether ANY hourly mechanism has an edge
net of cost, AND — the operator's actual question — whether a VOLATILE regime rescues it.

Three long-only hourly mechanisms (long-only per the standing NFLX-lesson guard):
  MR   mean-reversion fade : after an hourly bar drops <= -DOWN%, buy at that bar's close,
                             exit next hourly bar close. (Buy the intraday dip.)
  GAP  overnight gap-fade  : if today opens <= -GAP% vs prior close, buy at first-hour
                             close, exit day's last bar close.
  MOM  momentum continuation: after an hourly bar rises >= +UP%, buy at close, hold 1 bar.

For every trade we tag the entry bar's trailing realized vol (std of last 20 hourly
returns) and bucket trades into LOW / MID / HIGH vol terciles (per name). The headline
question: is HIGH-vol expectancy materially better than LOW-vol?

Net of a round-trip cost, bracketed at 2 / 5 / 10 bps. Per-trade t-stat = mean/std*sqrt(n)
is the honest robustness stat for irregularly-timed trades; a daily-aggregated Sharpe/|MDD|
is reported for color only (NOT gate-able).

Usage:  uv run python ledgers/improvements/2026-08-04-hourly-strategies-vol-regime-probe.py
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

TICKERS = ["SPY", "QQQ", "IWM", "NVDA", "TSLA", "AMD", "META", "AMZN", "COIN", "PLTR"]
COSTS_BPS = [2.0, 5.0, 10.0]      # round-trip
DOWN, UP, GAP = 1.0, 1.0, 1.0     # % thresholds for MR / MOM / GAP triggers
VOL_WIN = 20                      # trailing hourly bars for the realized-vol regime tag


def _load(ticker: str) -> pd.DataFrame | None:
    try:
        df = fetch_ohlcv(ticker, period="730d", interval="1h").df
    except Exception as e:
        print(f"  {ticker}: fetch failed — {e!r}", flush=True); return None
    if df is None or len(df) < 100:
        print(f"  {ticker}: too few bars ({0 if df is None else len(df)})", flush=True); return None
    cols = {c.lower(): c for c in df.columns}
    df = df.rename(columns={cols["open"]: "O", cols["high"]: "H", cols["low"]: "L", cols["close"]: "C"})
    df = df[["O", "H", "L", "C"]].astype(float)
    df.index = pd.to_datetime(df.index)
    df["ret"] = df["C"].pct_change()
    df["vol"] = df["ret"].rolling(VOL_WIN).std()
    df["_date"] = df.index.date
    return df


def trades_for(ticker: str, df: pd.DataFrame) -> pd.DataFrame:
    recs = []
    r = df["ret"].values; C = df["C"].values; vol = df["vol"].values
    # MR + MOM: 1-bar holds keyed on the prior bar's move
    for i in range(VOL_WIN + 1, len(df) - 1):
        nxt = (C[i + 1] / C[i]) - 1.0
        if r[i] <= -DOWN / 100.0:
            recs.append({"ticker": ticker, "mech": "MR", "date": pd.Timestamp(df["_date"].iloc[i]),
                         "raw": nxt, "vol": vol[i]})
        if r[i] >= UP / 100.0:
            recs.append({"ticker": ticker, "mech": "MOM", "date": pd.Timestamp(df["_date"].iloc[i]),
                         "raw": nxt, "vol": vol[i]})
    # GAP: fade a down-gap open, hold to end of day
    prev_close = None
    for d, g in df.groupby("_date"):
        g = g.sort_index()
        if prev_close is not None and len(g) >= 2:
            gap = (float(g["O"].iloc[0]) / prev_close) - 1.0
            if gap <= -GAP / 100.0:
                entry = float(g["C"].iloc[0]); exitp = float(g["C"].iloc[-1])
                recs.append({"ticker": ticker, "mech": "GAP", "date": pd.Timestamp(d),
                             "raw": (exitp / entry) - 1.0, "vol": float(g["vol"].iloc[0]) if not math.isnan(g["vol"].iloc[0]) else np.nan})
        prev_close = float(g["C"].iloc[-1])
    t = pd.DataFrame(recs)
    if not t.empty:
        # per-name vol terciles
        t["voltile"] = pd.qcut(t["vol"].rank(method="first"), 3, labels=["LOW", "MID", "HIGH"])
    return t


def stat(x: np.ndarray) -> tuple[float, float, float]:
    """mean%, hit%, t-stat."""
    if len(x) < 5 or np.std(x, ddof=1) == 0:
        return float(np.mean(x) * 100) if len(x) else 0.0, 0.0, 0.0
    return float(np.mean(x) * 100), float((x > 0).mean() * 100), float(np.mean(x) / np.std(x, ddof=1) * math.sqrt(len(x)))


def main():
    print("fetching ~730d hourly bars ...", flush=True)
    frames = []
    for tk in TICKERS:
        df = _load(tk)
        if df is not None:
            frames.append(trades_for(tk, df))
    allt = pd.concat([f for f in frames if not f.empty], ignore_index=True) if frames else pd.DataFrame()
    if allt.empty:
        print("NO hourly data retrieved — yfinance intraday unavailable/too shallow.")
        return
    span = f"{allt['date'].min().date()}..{allt['date'].max().date()}"
    lines = ["# Hourly strategies — vol-regime probe — RESEARCH-ONLY, NOT GATE-CLEARED (pilot, 2026-08-04)", "",
             "**Coarse 1h bars over ~2yr of yfinance data. CANNOT be walk-forward / DSR / net-cost gated "
             "(data too shallow + hourly too coarse). Directional pulse-check only — and specifically a test of "
             "whether a VOLATILE regime rescues any hourly mechanism, per the operator's question.**", "",
             f"- Universe {TICKERS} · window {span} · long-only · triggers MR<=-{DOWN}% / MOM>=+{UP}% / GAP<=-{GAP}%", ""]
    # headline table: per mechanism x cost
    lines += ["## Headline — per-trade expectancy, net of cost", "",
              "| mech | cost bps | n | avg net % | hit % | t-stat |", "|---|---|---|---|---|---|"]
    best_t = {}
    for mech in ["MR", "GAP", "MOM"]:
        sub = allt[allt["mech"] == mech]
        for c in COSTS_BPS:
            net = sub["raw"].values - c / 1e4
            m, h, t = stat(net)
            lines.append(f"| {mech} | {c:.0f} | {len(sub)} | {m:.4f} | {h:.1f} | {t:.2f} |")
        best_t[mech] = stat(sub["raw"].values - 5.0 / 1e4)[2]  # t at 5bps
    # vol-regime table (at 5 bps)
    lines += ["", "## Vol-regime split (net @ 5 bps round-trip) — DOES VOLATILITY HELP?", "",
              "| mech | vol regime | n | avg net % | hit % | t-stat |", "|---|---|---|---|---|---|"]
    regime_delta = {}
    for mech in ["MR", "GAP", "MOM"]:
        sub = allt[allt["mech"] == mech]
        row = {}
        for reg in ["LOW", "MID", "HIGH"]:
            s = sub[sub["voltile"] == reg]
            net = s["raw"].values - 5.0 / 1e4
            m, h, t = stat(net)
            row[reg] = m
            lines.append(f"| {mech} | {reg} | {len(s)} | {m:.4f} | {h:.1f} | {t:.2f} |")
        regime_delta[mech] = row.get("HIGH", 0.0) - row.get("LOW", 0.0)
    # daily-aggregated color (best mechanism)
    lines += ["", "## Verdict (indicative — NOT a gate verdict; cannot deploy off this)"]
    passes = [m for m, t in best_t.items() if t > 2.0]
    if passes:
        lines.append(f"- Mechanisms with a per-trade t-stat > 2 at 5bps: **{passes}** — worth a real intraday feed to test properly.")
    else:
        lines.append("- **No mechanism clears a per-trade t-stat > 2 at realistic (5bps) cost.** Hourly edge is not "
                     "distinguishable from noise net of cost in this window.")
    for m in ["MR", "GAP", "MOM"]:
        verdict = "HIGH-vol materially better" if regime_delta[m] > 0.02 else \
                  ("HIGH-vol worse/flat" if regime_delta[m] <= 0.0 else "marginal")
        lines.append(f"- {m}: HIGH−LOW vol expectancy delta = {regime_delta[m]:+.4f}% → {verdict}.")
    lines += ["- Suggest-only; nothing applied. A real hourly test needs a paid minute/hourly source (Alpaca/Polygon) "
              "with 8-10yr depth to run the actual gate.", ""]
    out = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-04-hourly-strategies-vol-regime-probe.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    # console
    print(f"\ntrades={len(allt)} span={span}")
    for mech in ["MR", "GAP", "MOM"]:
        sub = allt[allt["mech"] == mech]
        m, h, t = stat(sub["raw"].values - 5.0 / 1e4)
        hi = stat(sub[sub["voltile"] == "HIGH"]["raw"].values - 5.0 / 1e4)
        lo = stat(sub[sub["voltile"] == "LOW"]["raw"].values - 5.0 / 1e4)
        print(f"{mech:4} n={len(sub):5} @5bps avg={m:.4f}% hit={h:.1f}% t={t:.2f}  | HIGHvol avg={hi[0]:.4f}% t={hi[2]:.2f}  LOWvol avg={lo[0]:.4f}% t={lo[2]:.2f}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
