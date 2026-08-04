"""Options DELTA-HEDGED STRADDLE — daily-hedged simulation, research-only / suggest-only.
Claude1 paper-research pilot, 2026-08-04.

The operator asked for options with delta AND gamma hedging done properly. This simulates a
1-month ATM straddle, DELTA-HEDGED DAILY along the realized SPY path, so it captures the
actual gamma-scalping mechanics + discrete-hedge error (not just the variance-swap identity
of the earlier VRP proxy).

Method (standard pre-OptionMetrics research technique):
  - Each month: S0=SPY close, implied sigma = VIX/100, K=S0 (ATM), T=21/252, r=0.
  - Price call+put via Black-Scholes -> straddle premium.
  - SHORT the straddle; hold +straddle_delta shares of SPY to neutralize (delta hedge).
  - Each trading day to expiry: re-price the straddle at the realized S_t (implied sigma
    held fixed = sell-implied/hedge-at-implied convention), re-hedge delta, accrue P&L:
        dPnL = -(V_t - V_{t-1})            # short option mark-to-market
               + h_{t-1} * (S_t - S_{t-1}) # delta hedge
               - |dh| * S_t * HEDGE_BPS/1e4  # hedge rebalance cost
    At expiry V = intrinsic. Entry option premium haircut = OPT_SPREAD_PCT (option bid/ask).
  - LONG straddle (gamma scalp / long-vol) = the exact negative of the short P&L series.

Costs make this a REALISTIC (not idealized) read: option spread haircut + daily hedge
cost + discrete (daily, not continuous) hedging error are all included. Caveats: VIX is
30d SPX ATM IV (a proxy for a 21d SPY straddle IV; ignores skew), r=0, American/dividend
effects ignored. Research proxy, NOT deployable.

Usage:  uv run python ledgers/improvements/2026-08-04-options-delta-hedge-backtest.py
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
OOS_YEARS = [2020, 2021, 2022, 2023, 2024, 2025]
OPT_SPREAD_PCT = 1.0     # % of straddle premium, one-way entry haircut (option bid/ask)
HEDGE_BPS = 1.0          # per-share hedge trade, bps of notional
DAYS_T = 21              # trading days to expiry (~1 month)


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_straddle(S, K, T, sigma, r=0.0):
    """Return (value, delta) of a long ATM straddle (call+put)."""
    if T <= 1e-9 or sigma <= 1e-9:
        intr = abs(S - K)
        d = 1.0 if S > K else (-1.0 if S < K else 0.0)   # straddle delta at expiry
        return intr, d
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    call = S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    put = K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)
    delta = _ncdf(d1) + (_ncdf(d1) - 1.0)                 # call delta + put delta = 2N(d1)-1
    return call + put, delta


def _load_close(ticker):
    df = fetch_ohlcv(ticker, period="10y", interval="1d").df
    cmap = {c.lower(): c for c in df.columns}
    s = df[cmap["close"]].astype(float)
    ix = pd.to_datetime(df.index)
    if getattr(ix, "tz", None) is not None:
        ix = ix.tz_localize(None)
    s.index = ix.normalize()
    return s.sort_index().loc[START:END]


def simulate(spy, vix):
    idx = spy.index
    month_key = pd.Series(idx, index=idx).dt.to_period("M")
    rows = []
    for period, grp in pd.DataFrame({"s": spy, "v": vix}).groupby(month_key):
        g = grp.dropna()
        if len(g) < 15:
            continue
        S = g["s"].values.astype(float)
        sigma = float(g["v"].iloc[0]) / 100.0
        S0 = S[0]; K = S0
        # entry
        V0, d0 = bs_straddle(S0, K, DAYS_T / 252.0, sigma)
        prem_haircut = V0 * OPT_SPREAD_PCT / 100.0
        h = d0
        hedge_cost = abs(h) * S0 * HEDGE_BPS / 1e4
        hedge_pnl = 0.0
        Vprev = V0; Sprev = S0
        m = len(S)
        for k in range(1, m):
            T = max(DAYS_T - k, 0) / 252.0
            V, d = bs_straddle(S[k], K, T, sigma)
            hedge_pnl += h * (S[k] - Sprev)          # P&L from shares held over the step
            dh = d - h
            hedge_cost += abs(dh) * S[k] * HEDGE_BPS / 1e4
            h = d; Vprev = V; Sprev = S[k]
        V_exp, _ = bs_straddle(S[-1], K, 0.0, sigma)
        short_pnl = (V0 - V_exp) + hedge_pnl - hedge_cost - prem_haircut
        realized = float(pd.Series(np.log(S[1:] / S[:-1])).std(ddof=1) * math.sqrt(252))
        rows.append({"month": str(period), "implied": sigma, "realized": realized,
                     "short_pnl_per_notional": short_pnl / S0})
    return pd.DataFrame(rows)


def ann_sharpe_m(x):
    x = pd.Series(x)
    return float(x.mean() / x.std(ddof=1) * math.sqrt(12)) if x.std(ddof=1) > 0 else 0.0


def main():
    print("fetching SPY + ^VIX ...", flush=True)
    spy = _load_close("SPY"); vix = _load_close("^VIX")
    common = spy.index.intersection(vix.index)
    spy, vix = spy.loc[common], vix.loc[common]
    df = simulate(spy, vix)
    s = df["short_pnl_per_notional"]
    mean_m = float(s.mean()); std_m = float(s.std(ddof=1))
    sharpe = ann_sharpe_m(s); skew = float(((s - mean_m) ** 3).mean() / std_m ** 3) if std_m > 0 else 0.0
    hit = float((s > 0).mean() * 100)
    eq = (1 + s).cumprod(); mdd = float(((eq / eq.cummax()) - 1).min() * 100)
    per = {}
    for y in OOS_YEARS:
        sy = s[df["month"].str.startswith(str(y)).values]
        per[y] = ann_sharpe_m(sy) if len(sy) >= 6 else float("nan")
    worst = df.reindex(s.sort_values().index).head(5)[["month", "implied", "realized", "short_pnl_per_notional"]]

    lines = ["# Options delta-hedged straddle (daily hedge, BS off VIX) — proxy (pilot, 2026-08-04)", "",
             "**Research-only. 1-mo ATM straddle, delta-hedged DAILY along realized SPY path; option-spread "
             f"haircut {OPT_SPREAD_PCT:.0f}% + {HEDGE_BPS:.0f}bps/hedge + discrete-hedge error included. VIX as "
             "ATM-IV proxy, r=0, skew ignored. NOT deployable.**", "",
             f"- Monthly obs n={len(df)} ({df['month'].iloc[0]}..{df['month'].iloc[-1]})",
             f"- SHORT straddle (delta-hedged): mean {mean_m:+.5f} /notional/mo · **annualized Sharpe {sharpe:.2f}** · "
             f"hit {hit:.0f}% · **skew {skew:.2f}** · |MDD| {abs(mdd):.0f}%",
             f"- LONG straddle (gamma scalp, delta-hedged) = exact mirror: Sharpe {-sharpe:.2f}, skew {-skew:.2f} "
             "(positive), pays carry, wins the tail.", "",
             "### Per-OOS-year annualized Sharpe (short straddle)", "| year | Sharpe |", "|---|---|"]
    for y in OOS_YEARS:
        lines.append(f"| {y} | {per[y]:.2f} |")
    lines += ["", "### Worst 5 months (short straddle — the gamma tail you eat)",
              "| month | implied | realized | P&L/notional |", "|---|---|---|---|"]
    for _, r in worst.iterrows():
        lines.append(f"| {r['month']} | {r['implied']*100:.0f} | {r['realized']*100:.0f} | {r['short_pnl_per_notional']:+.5f} |")
    lines += ["", "## CRITICAL CAVEAT — this Sharpe is the OPTIMISTIC bracket",
              f"- Implied vol is held FIXED at the month-start VIX, so this captures only the pure GAMMA (realized-vol) "
              f"harvest held to expiry — it NEVER marks the intra-month VEGA spike. In 2020-03 VIX went 33->80 mid-month; "
              f"a real short straddle takes a mark-to-market / margin-call hit on that, which this sim omits. That is why "
              f"|MDD| here is only {abs(mdd):.0f}% and skew {skew:.2f} looks mild.",
              "- The companion **variance-swap proxy** (`2026-08-04-variance-risk-premium-probe`) is the PESSIMISTIC "
              "bracket: Sharpe 0.31, |MDD| 72%, skew -7.4 — closer to the margin-call experience.",
              f"- **The truth is bracketed between them:** the VRP gamma harvest has a genuinely decent Sharpe (~{sharpe:.1f}) "
              "IF you can survive the vega marks — but surviving the vega marks (and margin) is exactly what destroys "
              "short-vol books in a crash. Neither number alone is honest; the pair is.",
              "## Read",
              "- Long gamma (the 'volatile market' play) is the mirror: pays every calm month, wins only in vol spikes.",
              "- Not expressible in the equity-only, long-only stack; needs option-chain data + a hedge engine + an "
              "options broker + a negative-gamma/margin risk regime. See the feasibility scope.",
              "- Suggest-only; nothing applied."]
    out = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-04-options-delta-hedge-backtest.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nn={len(df)} short-straddle Sharpe={sharpe:.2f} skew={skew:.2f} hit={hit:.0f}% |MDD|={abs(mdd):.0f}%")
    print("per-year:", {y: round(per[y], 2) for y in OOS_YEARS})
    print("worst months:"); print(worst.to_string(index=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
