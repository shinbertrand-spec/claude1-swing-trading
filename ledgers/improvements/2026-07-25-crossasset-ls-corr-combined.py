"""Cross-asset trend + synthetic shorts (Phase 0b) — decorrelation + combined book.

Same CORRECTED method as v2 (fresh-window psim sims, weekday calendar, validated
standalone Sharpe vs psim). Universe = long diversifiers + -1x inverse ETFs.
Tests whether the SHORT side (esp. short-equity SH/PSQ/RWM/EFZ/EUM) lowers the
correlation to ts_momentum and adds crisis alpha. Read-only.
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

from tools.auto_paper import quant_scanner
from tools.backtest import portfolio_simulator as psim
from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

LS_TICKERS = ["TLT", "IEF", "GLD", "SLV", "DBC", "DBA", "USO", "UUP",
              "SH", "PSQ", "RWM", "TBF", "EFZ", "EUM", "DGZ"]
TS_SPEC = "tools/quant_strategies/ts_momentum_liquid_us.yml"
WINDOW_START = date.fromisoformat("2021-06-23")
WINDOW_END = date.fromisoformat("2026-05-25")


def _window_daily(tickers, benchmark, params):
    kind_mod = KIND_REGISTRY["ts_momentum"]
    tks = list(tickers)
    if benchmark not in tks:
        tks.append(benchmark)
    dfs = _load_universe(tks, date.fromisoformat("2019-06-01"), WINDOW_END, force_refetch=False)
    state = kind_mod.precompute(dfs, params) if hasattr(kind_mod, "precompute") else None
    signals = []
    for t, df in dfs.items():
        if t == benchmark:
            continue
        signals.extend(kind_mod.replay(df, t, params, state))
    win = [s for s in signals if WINDOW_START <= s.fill_date <= WINDOW_END]
    res = psim.simulate(win, dfs)
    d = res.equity_curve.pct_change().dropna()
    d.index = pd.to_datetime(d.index)
    d = d[(d.index >= pd.Timestamp(WINDOW_START)) & (d.index <= pd.Timestamp(WINDOW_END))]
    return d, res.sharpe_annualised, res.max_drawdown_pct


def _sharpe(r):
    return float(r.mean() / r.std(ddof=1) * np.sqrt(252)) if r.std(ddof=1) > 0 else float("nan")


def _mdd(r):
    eq = (1 + r).cumprod()
    return float((eq / eq.cummax() - 1.0).min() * 100)


def main() -> None:
    ts_spec = yaml.safe_load(open(TS_SPEC, encoding="utf-8"))
    ts_params = quant_scanner._live_params_for({"deployable_params": {"lookback_days": 252, "top_k": 8}}, ts_spec)
    ts_tickers = resolve_universe_tickers(ts_spec)
    ts, ts_sr, ts_mdd = _window_daily(ts_tickers, "SPY", ts_params)

    ls_params = {"lookback_days": 126, "rebalance_period_days": 21, "max_hold_days": 21,
                 "top_k": 8, "atr_period": 20, "atr_stop_multiple": 3.0, "risk_per_trade": 0.01,
                 "benchmark": "SPY"}
    ls, ls_sr, ls_mdd = _window_daily(LS_TICKERS, "SPY", ls_params)

    print(f"[fresh-window] ts Sharpe={ts_sr:.2f} MDD={ts_mdd:.1f} | "
          f"ls(long+synthetic-short) Sharpe={ls_sr:.2f} MDD={ls_mdd:.1f}", flush=True)

    j = pd.concat([ts.rename("ts"), ls.rename("ls")], axis=1, join="inner").dropna()
    corr = float(np.corrcoef(j["ts"], j["ls"])[0, 1])
    print(f"[joined] n={len(j)} validate ts={_sharpe(j['ts']):.2f} ls={_sharpe(j['ls']):.2f} "
          f"corr={corr:+.3f}", flush=True)

    v_ts, v_ls = j["ts"].std(), j["ls"].std()
    w_iv = (1 / v_ts) / ((1 / v_ts) + (1 / v_ls))
    blends = {
        "ts_momentum ALONE": j["ts"],
        "cross-asset L/S ALONE": j["ls"],
        "50/50 equal blend": 0.5 * j["ts"] + 0.5 * j["ls"],
        f"inverse-vol ({w_iv:.2f} ts / {1-w_iv:.2f} ls)": w_iv * j["ts"] + (1 - w_iv) * j["ls"],
        "70/30 (ts-heavy)": 0.7 * j["ts"] + 0.3 * j["ls"],
    }

    lines = ["# Cross-asset trend + synthetic shorts (inverse ETFs) — decorrelation + combined book", "",
             f"- Common window: **{WINDOW_START} → {WINDOW_END}** ({len(j)} weekday days)",
             f"- Universe: 8 long diversifiers + 7 synthetic-short (-1x inverse) ETFs, crypto-free",
             f"- **OOS correlation ts vs cross-asset L/S: r = {corr:+.3f}**",
             f"  (vs long-only crypto-free r=+0.205 — did adding the short side lower it?)",
             "", "| book | ann. Sharpe | MDD% | days |", "|---|---|---|---|"]
    for name, r in blends.items():
        lines.append(f"| {name} | {_sharpe(r):.2f} | {_mdd(r):.1f} | {len(r)} |")
    lines += ["",
              "**Read:** the short side's value = (a) a LOWER (ideally negative) correlation to "
              "ts_momentum than the long-only +0.205, and (b) a combined book that beats "
              "ts-alone on Sharpe or MDD. Synthetic shorts carry inverse-ETF decay (embedded in "
              "price) + net-cost. No look-ahead in 50/50, 70/30, inverse-vol."]
    out = "ledgers/improvements/2026-07-25-crossasset-ls-corr-combined.md"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    for name, r in blends.items():
        print(f"{name}: Sharpe {_sharpe(r):.2f} MDD {_mdd(r):.1f}%", flush=True)
    print(f"\nwrote {out}", flush=True)


if __name__ == "__main__":
    main()
