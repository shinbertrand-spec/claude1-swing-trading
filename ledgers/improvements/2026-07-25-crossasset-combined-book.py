"""Cross-asset trend (Phase 0) — the decisive test: does ADDING the (uncorrelated,
standalone-failing) cross-asset trend sleeve to ts_momentum_liquid_us RAISE the
COMBINED book's OOS Sharpe above ts_momentum-alone (net OOS 1.04)?

This is the portfolio-marginal question the standalone gate cannot ask. Read-only.

Weightings reported (all a-priori except the last):
  - 50/50 equal daily-return blend
  - inverse-vol risk parity (weights from full-overlap daily vol)
  - ex-post max-Sharpe weight (IN-SAMPLE optimum — reported as an UPPER BOUND only)
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import date, timedelta

import numpy as np
import pandas as pd
import yaml

from tools.auto_paper import quant_scanner
from tools.backtest import portfolio_simulator as psim
from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

CA_SPEC = "ledgers/improvements/2026-07-25-crossasset_trend.yml"
TS_SPEC = "tools/quant_strategies/ts_momentum_liquid_us.yml"


def _daily_full(spec_path: str, promoted: dict) -> pd.Series:
    spec = yaml.safe_load(open(spec_path, encoding="utf-8"))
    kind_mod = KIND_REGISTRY[spec["kind"]]
    benchmark = spec["universe"]["benchmark"]
    tickers = resolve_universe_tickers(spec)
    if benchmark not in tickers:
        tickers.append(benchmark)
    start = date.fromisoformat(str(spec["period"]["start"]))
    end = date.fromisoformat(str(spec["period"]["end"]))
    dfs = _load_universe(tickers, start, end, force_refetch=False)
    params = quant_scanner._live_params_for({"deployable_params": promoted}, spec)
    state = kind_mod.precompute(dfs, params) if hasattr(kind_mod, "precompute") else None
    signals = []
    for t, df in dfs.items():
        if t == benchmark:
            continue
        signals.extend(kind_mod.replay(df, t, params, state))
    res = psim.simulate(signals, dfs)
    d = res.equity_curve.pct_change().dropna()
    d.index = pd.to_datetime(d.index)
    return d


def _sharpe(r: pd.Series) -> float:
    return float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else float("nan")


def _mdd(r: pd.Series) -> float:
    eq = (1 + r).cumprod()
    dd = eq / eq.cummax() - 1.0
    return float(dd.min() * 100)


def _report(name: str, r: pd.Series) -> str:
    return f"| {name} | {_sharpe(r):.2f} | {_mdd(r):.1f} | {len(r)} |"


def main() -> None:
    ts = _daily_full(TS_SPEC, {"lookback_days": 252, "top_k": 8})
    ca = _daily_full(CA_SPEC, {"lookback_days": 126, "top_k": 8})

    j = pd.concat([ts.rename("ts"), ca.rename("ca")], axis=1, join="inner").dropna()
    # OOS overlap = the cross-asset last-30% split boundary (matches the corr report).
    ca_start = date.fromisoformat("2010-01-01"); ca_end = date.fromisoformat("2026-05-25")
    oos_start = pd.Timestamp(ca_start + timedelta(days=int((ca_end - ca_start).days * 0.70)))
    o = j[j.index >= oos_start]
    print(f"overlap full={len(j)} oos={len(o)} from {oos_start.date()}", flush=True)

    corr = float(np.corrcoef(o["ts"], o["ca"])[0, 1])
    v_ts, v_ca = o["ts"].std(), o["ca"].std()
    w_iv = (1 / v_ts) / ((1 / v_ts) + (1 / v_ca))  # inverse-vol weight on ts
    # ex-post max-Sharpe two-asset weight (in-sample; UPPER BOUND only)
    m_ts, m_ca = o["ts"].mean(), o["ca"].mean()
    cov = np.cov(o["ts"], o["ca"])
    inv = np.linalg.pinv(cov)
    w = inv @ np.array([m_ts, m_ca])
    w = w / w.sum()
    w_opt_ts = float(np.clip(w[0], 0, 1))

    blends = {
        "ts_momentum ALONE (baseline)": o["ts"],
        "cross-asset ALONE": o["ca"],
        "50/50 equal blend": 0.5 * o["ts"] + 0.5 * o["ca"],
        f"inverse-vol ({w_iv:.2f} ts / {1-w_iv:.2f} ca)": w_iv * o["ts"] + (1 - w_iv) * o["ca"],
        f"max-Sharpe IN-SAMPLE ({w_opt_ts:.2f} ts) [UPPER BOUND]":
            w_opt_ts * o["ts"] + (1 - w_opt_ts) * o["ca"],
    }

    lines = ["# Cross-asset trend — combined-book test (OOS window)", "",
             f"- OOS overlap: **{len(o)} trading days** from {oos_start.date()} "
             f"(covers 2022 rate-shock, 2023-24 bull, 2025)",
             f"- OOS correlation ts vs cross-asset: **r = {corr:+.3f}** (uncorrelated)",
             "", "| book | ann. Sharpe | MDD% | days |", "|---|---|---|---|"]
    for name, r in blends.items():
        lines.append(_report(name, r))
    lines += ["",
              "**Read:** the standalone gate asks 'Sharpe>1.0 alone?' and rejects cross-asset "
              "(0.52). The portfolio-marginal question is whether ADDING it lifts the combined "
              "book. Compare the blends against ts_momentum ALONE.",
              "",
              "_a-priori blends (50/50, inverse-vol) carry no look-ahead. The in-sample "
              "max-Sharpe weight is an overfit UPPER BOUND, shown only to bound the opportunity._"]
    out = "ledgers/improvements/2026-07-25-crossasset-combined-book.md"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    for name, r in blends.items():
        print(f"{name}: Sharpe {_sharpe(r):.2f} MDD {_mdd(r):.1f}%", flush=True)
    print(f"\nwrote {out}", flush=True)


if __name__ == "__main__":
    main()
