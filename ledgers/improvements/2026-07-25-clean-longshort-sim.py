"""Clean DECAY-FREE long/short trend simulator (research-only, no live orders).

Answers: does the SHORT side of cross-asset trend pay when implemented cleanly
(real short futures — no inverse-ETF decay, no expense drag)?

Design (deliberately simple + auditable to avoid the v1 measurement bug):
  - Master calendar = benchmark (SPY) trading days; each ticker's Close reindexed.
  - Trend signal at each rebalance: trailing `lookback`-day return per ticker.
  - long-only mode: hold top_k by trailing return among POSITIVE-trend names, +1/k each.
  - long/short mode: hold top_k by |trailing return|, sign(trailing)/k each (a short
    is -1/k — clean short P&L = -(asset return), NO daily-reset decay).
  - Positions held between 21-day rebalances. Gross exposure = 1.0 both modes.
  - Costs: cost_bps on rebalance turnover (|Δsigned weight|) + borrow_bps/yr on short
    notional daily. Futures roll NOT modeled (small; noted).

VALIDATION GATE: ts_momentum run through THIS sim (long-only, lookback 252) must
reproduce ~1.0-1.4 Sharpe (its psim figure) or the sim is not trusted.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import date

import numpy as np
import pandas as pd
import yaml

from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

BENCH = "SPY"
LOAD_START = date.fromisoformat("2016-06-01")   # lookback-252 warmup before eval
EVAL_START = pd.Timestamp("2018-01-01")
EVAL_END = pd.Timestamp("2026-05-25")

CA_TICKERS = ["TLT", "IEF", "GLD", "SLV", "DBC", "DBA", "USO", "UUP",   # diversifiers
              "SPY", "QQQ", "IWM", "EFA", "EEM"]                        # equity indices (short in crashes)


def _price_matrix(tickers, load_end=EVAL_END):
    dfs = _load_universe(list(dict.fromkeys(tickers + [BENCH])), LOAD_START, load_end.date(), False)
    cal = dfs[BENCH].index
    cols = {}
    for t in tickers:
        if t in dfs and len(dfs[t]):
            s = dfs[t]["Close"].reindex(cal)
            cols[t] = s
    px = pd.DataFrame(cols)
    px.index = pd.to_datetime(px.index)
    if px.index.tz is not None:
        px.index = px.index.tz_localize(None)
    return px


def run(px: pd.DataFrame, *, lookback: int, rebal: int, top_k: int, allow_short: bool,
        cost_bps: float = 8.0, borrow_bps_yr: float = 30.0) -> dict:
    rets = px.pct_change()
    dates = px.index
    n = len(dates)
    tickers = list(px.columns)
    w = pd.Series(0.0, index=tickers)         # current signed weights
    daily = []
    idx_dates = []
    reb_idx = set(range(lookback, n, rebal))
    avg_pos, avg_short, turns = [], [], []
    for i in range(lookback, n):
        d = dates[i]
        # rebalance at start of the bar (positions apply to today's return)
        if i in reb_idx:
            trailing = px.iloc[i] / px.iloc[i - lookback] - 1.0
            trailing = trailing.dropna()
            if len(trailing):
                if allow_short:
                    ranked = trailing.reindex(trailing.abs().sort_values(ascending=False).index)
                    sel = ranked.iloc[:top_k]
                    neww = pd.Series(0.0, index=tickers)
                    for t, v in sel.items():
                        neww[t] = (1.0 if v > 0 else -1.0) / top_k
                else:
                    pos = trailing[trailing > 0].sort_values(ascending=False)
                    sel = pos.iloc[:top_k]
                    neww = pd.Series(0.0, index=tickers)
                    for t in sel.index:
                        neww[t] = 1.0 / top_k
                turn = float((neww - w).abs().sum())
                turns.append(turn)
                w = neww
            avg_pos.append(int((w != 0).sum()))
            avg_short.append(int((w < 0).sum()))
        r_today = float((w * rets.iloc[i].fillna(0.0)).sum())
        # borrow cost on short notional (daily), applied every day
        short_notional = float(w[w < 0].abs().sum())
        r_today -= short_notional * (borrow_bps_yr / 1e4) / 252.0
        # turnover cost only on rebalance days
        if i in reb_idx and turns:
            r_today -= turns[-1] * (cost_bps / 1e4)
        daily.append(r_today)
        idx_dates.append(d)
    s = pd.Series(daily, index=pd.DatetimeIndex(idx_dates))
    s = s[(s.index >= EVAL_START) & (s.index <= EVAL_END)]
    sr = float(s.mean() / s.std(ddof=1) * np.sqrt(252)) if s.std(ddof=1) > 0 else float("nan")
    eq = (1 + s).cumprod()
    mdd = float((eq / eq.cummax() - 1.0).min() * 100)
    return {"daily": s, "sharpe": sr, "mdd": mdd,
            "avg_positions": float(np.mean(avg_pos)) if avg_pos else 0,
            "avg_shorts": float(np.mean(avg_short)) if avg_short else 0,
            "avg_turnover": float(np.mean(turns)) if turns else 0}


def _sr(s):
    return float(s.mean() / s.std(ddof=1) * np.sqrt(252)) if s.std(ddof=1) > 0 else float("nan")


def _mdd(s):
    eq = (1 + s).cumprod()
    return float((eq / eq.cummax() - 1.0).min() * 100)


def main() -> None:
    # ---- VALIDATION: ts_momentum through this sim (long-only, lookback 252) ----
    ts_spec = yaml.safe_load(open("tools/quant_strategies/ts_momentum_liquid_us.yml", encoding="utf-8"))
    ts_tickers = resolve_universe_tickers(ts_spec)
    ts_px = _price_matrix(ts_tickers)
    print(f"[load] ts universe cols={ts_px.shape[1]}  dates={ts_px.shape[0]}", flush=True)
    ts_run = run(ts_px, lookback=252, rebal=21, top_k=8, allow_short=False)
    print(f"[VALIDATION] ts_momentum (my-sim, long-only): Sharpe={ts_run['sharpe']:.2f} "
          f"MDD={ts_run['mdd']:.1f} avg_pos={ts_run['avg_positions']:.1f}  "
          f"(psim ref ~1.0-1.4)", flush=True)

    # ---- A/B: cross-asset long-only vs long/short (same sim, universe, costs) ----
    ca_px = _price_matrix(CA_TICKERS)
    print(f"[load] cross-asset cols={ca_px.shape[1]} dates={ca_px.shape[0]}", flush=True)
    ca_long = run(ca_px, lookback=126, rebal=21, top_k=8, allow_short=False)
    ca_ls = run(ca_px, lookback=126, rebal=21, top_k=8, allow_short=True)
    print(f"[A] cross-asset LONG-ONLY : Sharpe={ca_long['sharpe']:.2f} MDD={ca_long['mdd']:.1f} "
          f"avg_pos={ca_long['avg_positions']:.1f}", flush=True)
    print(f"[B] cross-asset LONG/SHORT: Sharpe={ca_ls['sharpe']:.2f} MDD={ca_ls['mdd']:.1f} "
          f"avg_pos={ca_ls['avg_positions']:.1f} avg_shorts={ca_ls['avg_shorts']:.1f}", flush=True)

    # ---- correlation + vol-matched combined vs ts (all my-sim, consistent) ----
    ts_s = ts_run["daily"]
    TARGET_VOL_D = 0.10 / np.sqrt(252)   # scale each sleeve to 10% annual vol (equal-risk, realistic)
    def znorm(s):
        return s / s.std(ddof=1) * TARGET_VOL_D
    rows = []
    for name, ca in (("long-only", ca_long), ("long/short", ca_ls)):
        j = pd.concat([ts_s.rename("ts"), ca["daily"].rename("ca")], axis=1, join="inner").dropna()
        corr = float(np.corrcoef(j["ts"], j["ca"])[0, 1])
        zt, zc = znorm(j["ts"]), znorm(j["ca"])
        blend = 0.5 * zt + 0.5 * zc
        rows.append((name, ca["sharpe"], ca["mdd"], corr, _sr(blend), _mdd(blend), len(j)))
        print(f"[combine {name}] corr={corr:+.3f} | ca standalone Sharpe={ca['sharpe']:.2f} | "
              f"equal-risk 50/50 w/ ts: Sharpe={_sr(blend):.2f} MDD={_mdd(blend):.1f}", flush=True)

    ts_solo_sr = _sr(znorm(ts_s[(ts_s.index >= EVAL_START)]))
    lines = ["# Clean decay-free long/short cross-asset trend — research backtest", "",
             f"- Eval window: {EVAL_START.date()} → {EVAL_END.date()} (incl. 2020 COVID + 2022 rate-shock)",
             f"- Cross-asset underlyings (both directions in L/S mode): {', '.join(CA_TICKERS)}",
             f"- Clean shorts: P&L = -(asset return), NO decay; costs = {8} bps turnover + {30} bps/yr borrow; futures roll not modeled",
             f"- **VALIDATION:** ts_momentum through this sim (long-only) Sharpe = **{ts_run['sharpe']:.2f}** "
             f"(psim ref ~1.0-1.4 — {'PASS' if 0.8 <= ts_run['sharpe'] <= 1.6 else 'CHECK'})",
             "",
             "## Standalone (my-sim, gross=1)", "",
             "| cross-asset mode | Sharpe | MDD% | avg positions | avg shorts |",
             "|---|---|---|---|---|",
             f"| long-only | {ca_long['sharpe']:.2f} | {ca_long['mdd']:.1f} | {ca_long['avg_positions']:.1f} | 0 |",
             f"| **long/short (clean)** | {ca_ls['sharpe']:.2f} | {ca_ls['mdd']:.1f} | {ca_ls['avg_positions']:.1f} | {ca_ls['avg_shorts']:.1f} |",
             "",
             "## Correlation to ts_momentum + equal-risk 50/50 combined book", "",
             "| cross-asset mode | corr → ts | combined Sharpe | combined MDD% |",
             "|---|---|---|---|",
             ]
    for name, casr, camdd, corr, bsr, bmdd, nobs in rows:
        lines.append(f"| {name} | {corr:+.3f} | {bsr:.2f} | {bmdd:.1f} |")
    lines += ["",
              f"_ts_momentum solo (equal-risk basis) Sharpe ≈ {ts_solo_sr:.2f} over this window._",
              "",
              "**Decision rule:** clean shorts PAY iff the long/short row shows (a) a LOWER "
              "(ideally negative) corr than long-only AND (b) a combined book that beats both "
              "ts-alone and the long-only-combined on Sharpe or MDD. Otherwise the short side "
              "does not justify the futures-broker + long-only-doctrine-reversal cost."]
    out = "ledgers/improvements/2026-07-25-clean-longshort-sim.md"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nwrote {out}", flush=True)


if __name__ == "__main__":
    main()
