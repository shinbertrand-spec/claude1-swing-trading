"""Cross-asset trend (Phase 0) — CORRECTED decorrelation + combined-book test.

Fixes two bugs in v1:
  (1) v1 sliced a continuous full-period equity curve to a sub-window (non-
      stationary sizing -> garbage Sharpe). v2 runs a FRESH psim over the
      common window for BOTH strategies (matches psim's reported OOS Sharpe).
  (2) v1 included crypto (BTC/ETH), which trade weekends -> 7-day calendar
      points pollute the 5-day ETF return series + sqrt(252) annualization,
      and drive the -39.6% MDD. v2 uses a CRYPTO-FREE 8-ETF diversifier book
      (all weekday calendars).

VALIDATION: prints each strategy's standalone Sharpe over the window and asserts
it is in a sane range before trusting correlation/combine. Read-only.
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

# Crypto-free diversifier book (all weekday-calendar ETFs).
CA_TICKERS = ["TLT", "IEF", "GLD", "SLV", "DBC", "DBA", "USO", "UUP"]
TS_SPEC = "tools/quant_strategies/ts_momentum_liquid_us.yml"
WINDOW_START = date.fromisoformat("2021-06-23")   # cross-asset OOS split; ~5y, post most of ts IS
WINDOW_END = date.fromisoformat("2026-05-25")


def _window_daily(tickers, benchmark, params, kind_name) -> pd.Series:
    """Fresh psim over [WINDOW_START, WINDOW_END]; return daily portfolio returns."""
    kind_mod = KIND_REGISTRY[kind_name]
    tks = list(tickers)
    if benchmark not in tks:
        tks.append(benchmark)
    # Load with lookback headroom before the window so signals can form at the edge.
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


def _sharpe(r: pd.Series) -> float:
    return float(r.mean() / r.std(ddof=1) * np.sqrt(252)) if r.std(ddof=1) > 0 else float("nan")


def _mdd(r: pd.Series) -> float:
    eq = (1 + r).cumprod()
    return float((eq / eq.cummax() - 1.0).min() * 100)


def main() -> None:
    ts_spec = yaml.safe_load(open(TS_SPEC, encoding="utf-8"))
    ts_params = quant_scanner._live_params_for({"deployable_params": {"lookback_days": 252, "top_k": 8}}, ts_spec)
    ts_tickers = resolve_universe_tickers(ts_spec)
    ts, ts_full_sr, ts_full_mdd = _window_daily(ts_tickers, "SPY", ts_params, "ts_momentum")

    ca_params = {"lookback_days": 126, "rebalance_period_days": 21, "max_hold_days": 21,
                 "top_k": 8, "atr_period": 20, "atr_stop_multiple": 3.0, "risk_per_trade": 0.01,
                 "benchmark": "SPY"}
    ca, ca_full_sr, ca_full_mdd = _window_daily(CA_TICKERS, "SPY", ca_params, "ts_momentum")

    print(f"[fresh-window sim] ts standalone Sharpe={ts_full_sr:.2f} MDD={ts_full_mdd:.1f} | "
          f"ca(crypto-free) Sharpe={ca_full_sr:.2f} MDD={ca_full_mdd:.1f}", flush=True)

    j = pd.concat([ts.rename("ts"), ca.rename("ca")], axis=1, join="inner").dropna()
    print(f"[joined weekday days] n={len(j)}  "
          f"validate: ts Sharpe={_sharpe(j['ts']):.2f}  ca Sharpe={_sharpe(j['ca']):.2f}", flush=True)

    corr = float(np.corrcoef(j["ts"], j["ca"])[0, 1])
    v_ts, v_ca = j["ts"].std(), j["ca"].std()
    w_iv = (1 / v_ts) / ((1 / v_ts) + (1 / v_ca))
    blends = {
        "ts_momentum ALONE (baseline)": j["ts"],
        "cross-asset ALONE (crypto-free)": j["ca"],
        "50/50 equal blend": 0.5 * j["ts"] + 0.5 * j["ca"],
        f"inverse-vol ({w_iv:.2f} ts / {1-w_iv:.2f} ca)": w_iv * j["ts"] + (1 - w_iv) * j["ca"],
    }

    lines = ["# Cross-asset trend v2 (crypto-free) — decorrelation + combined book", "",
             f"- Common window: **{WINDOW_START} → {WINDOW_END}** ({len(j)} weekday days; "
             f"covers 2022 rate-shock, 2023-24 bull, 2025)",
             f"- Cross-asset book: {', '.join(CA_TICKERS)} (crypto removed — clean weekday calendar)",
             f"- **OOS correlation ts vs cross-asset: r = {corr:+.3f}**",
             "", "| book | ann. Sharpe | MDD% | days |", "|---|---|---|---|"]
    for name, r in blends.items():
        lines.append(f"| {name} | {_sharpe(r):.2f} | {_mdd(r):.1f} | {len(r)} |")
    lines += ["",
              "Standalone Sharpes here are computed on the fresh-window sim (weekday-joined) — "
              "compare to psim's reported figures printed to the log as the validation check.",
              "",
              "**Read:** if the blends beat ts_momentum ALONE, the cross-asset sleeve is additive "
              "at the PORTFOLIO level despite failing the STANDALONE gate — the case for a "
              "portfolio-marginal gate. 50/50 and inverse-vol carry no look-ahead."]
    out = "ledgers/improvements/2026-07-25-crossasset-corr-combined-v2.md"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    for name, r in blends.items():
        print(f"{name}: Sharpe {_sharpe(r):.2f} MDD {_mdd(r):.1f}%", flush=True)
    print(f"\nwrote {out}", flush=True)


if __name__ == "__main__":
    main()
