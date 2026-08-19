"""Portfolio-marginal gate v2 — Sharpe-proportional weight (pre-registered).

ONLY change from v1: combine weight = walk-forward Sharpe-proportional (weight ∝
max(trailing-year Sharpe, 0), vol-matched sleeves) instead of 50/50. Gate clauses
M1-M4 and thresholds UNCHANGED. See 2026-07-25-portfolio-marginal-gate-v2-SCOPE.md.
Read-only.
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

TS_SPEC = "tools/quant_strategies/ts_momentum_liquid_us.yml"
CA_TICKERS = ["TLT", "IEF", "GLD", "SLV", "DBC", "DBA", "USO", "UUP"]
TARGET_VOL_D = 0.10 / np.sqrt(252)
YEARS = list(range(2018, 2026))
OOS_YEARS = list(range(2019, 2026))


def _signals(tickers, benchmark, params):
    kind_mod = KIND_REGISTRY["ts_momentum"]
    tks = list(dict.fromkeys(tickers + [benchmark]))
    dfs = _load_universe(tks, date.fromisoformat("2016-06-01"), date.fromisoformat("2026-05-25"), False)
    state = kind_mod.precompute(dfs, params) if hasattr(kind_mod, "precompute") else None
    sig = []
    for t, df in dfs.items():
        if t == benchmark:
            continue
        sig.extend(kind_mod.replay(df, t, params, state))
    return sig, dfs


def _year_daily(signals, dfs, y):
    lo, hi = date(y, 1, 1), date(y, 12, 31)
    win = [s for s in signals if lo <= s.fill_date <= hi]
    if not win:
        return pd.Series(dtype=float)
    res = psim.simulate(win, dfs)
    d = res.equity_curve.pct_change().dropna()
    d.index = pd.to_datetime(d.index)
    if d.index.tz is not None:
        d.index = d.index.tz_localize(None)
    return d[(d.index >= pd.Timestamp(lo)) & (d.index <= pd.Timestamp(hi))]


def _sharpe(s):
    return float(s.mean() / s.std(ddof=1) * np.sqrt(252)) if len(s) > 2 and s.std(ddof=1) > 0 else float("nan")


def _mdd(s):
    eq = (1 + s).cumprod()
    return float((eq / eq.cummax() - 1.0).min() * 100)


def _calmar(s):
    if len(s) < 2:
        return float("nan")
    eq = (1 + s).cumprod()
    yrs = len(s) / 252.0
    ann = eq.iloc[-1] ** (1 / yrs) - 1.0
    mdd = abs((eq / eq.cummax() - 1.0).min())
    return float(ann / mdd) if mdd > 1e-9 else float("nan")


def main() -> None:
    ts_spec = yaml.safe_load(open(TS_SPEC, encoding="utf-8"))
    ts_params = quant_scanner._live_params_for({"deployable_params": {"lookback_days": 252, "top_k": 8}}, ts_spec)
    ts_sig, ts_dfs = _signals(resolve_universe_tickers(ts_spec), "SPY", ts_params)
    ca_params = {"lookback_days": 126, "rebalance_period_days": 21, "max_hold_days": 21,
                 "top_k": 8, "atr_period": 20, "atr_stop_multiple": 3.0, "risk_per_trade": 0.01,
                 "benchmark": "SPY"}
    ca_sig, ca_dfs = _signals(CA_TICKERS, "SPY", ca_params)
    print(f"[load] ts signals={len(ts_sig)} ca signals={len(ca_sig)}", flush=True)

    ts_y = {y: _year_daily(ts_sig, ts_dfs, y) for y in YEARS}
    ca_y = {y: _year_daily(ca_sig, ca_dfs, y) for y in YEARS}

    book_parts, comb_parts, ts_sc_parts, ca_sc_parts = [], [], [], []
    per_window = []
    for y in OOS_YEARS:
        ts_prev, ca_prev = ts_y.get(y - 1), ca_y.get(y - 1)
        ts_cur, ca_cur = ts_y.get(y), ca_y.get(y)
        if any(x is None or len(x) < 20 for x in (ts_prev, ca_prev, ts_cur, ca_cur)):
            print(f"[skip {y}] insufficient data", flush=True)
            continue
        # v2 weight rule: Sharpe-proportional from trailing year, clamped >=0
        sr_ts_prev = max(_sharpe(ts_prev), 0.0)
        sr_ca_prev = max(_sharpe(ca_prev), 0.0)
        tot = sr_ts_prev + sr_ca_prev
        if tot <= 0:
            w_ts, w_ca = 0.5, 0.5
        else:
            w_ts, w_ca = sr_ts_prev / tot, sr_ca_prev / tot
        ts_scale = TARGET_VOL_D / ts_prev.std(ddof=1)
        ca_scale = TARGET_VOL_D / ca_prev.std(ddof=1)
        j = pd.concat([(ts_cur * ts_scale).rename("ts"), (ca_cur * ca_scale).rename("ca")],
                      axis=1, join="inner").dropna()
        book = j["ts"]
        comb = w_ts * j["ts"] + w_ca * j["ca"]
        book_parts.append(book); comb_parts.append(comb)
        ts_sc_parts.append(j["ts"]); ca_sc_parts.append(j["ca"])
        bc, cc = _calmar(book), _calmar(comb)
        per_window.append((y, w_ca, _sharpe(book), _sharpe(comb), bc, cc, _mdd(book), _mdd(comb)))
        print(f"[{y}] w_ca={w_ca:.2f} | book Sharpe={_sharpe(book):.2f} Calmar={bc:.2f} | "
              f"comb Sharpe={_sharpe(comb):.2f} Calmar={cc:.2f}", flush=True)

    book_agg = pd.concat(book_parts); comb_agg = pd.concat(comb_parts)
    ts_sc = pd.concat(ts_sc_parts); ca_sc = pd.concat(ca_sc_parts)
    b_sr, c_sr = _sharpe(book_agg), _sharpe(comb_agg)
    b_cal, c_cal = _calmar(book_agg), _calmar(comb_agg)
    b_mdd, c_mdd = _mdd(book_agg), _mdd(comb_agg)
    corr = float(np.corrcoef(ts_sc, ca_sc)[0, 1])
    win_improve = sum(1 for r in per_window if r[5] > r[4])
    n_win = len(per_window)

    m1 = c_sr >= 0.90 * b_sr
    m2 = c_cal >= 1.10 * b_cal
    m3 = (win_improve / n_win) >= 0.50 if n_win else False
    m4 = corr < 0.70
    passed = m1 and m2 and m3 and m4
    valid = 0.8 <= b_sr <= 1.6

    lines = ["# Portfolio-marginal gate v2 (Sharpe-proportional weight) — cross-asset diversifier", "",
             f"- Book B = ts_momentum_liquid_us · Candidate C = cross-asset long-only (crypto-free)",
             f"- Rolling 1-yr OOS 2019-2025 · weight = walk-forward Sharpe-proportional (trailing-yr)",
             f"- **VALIDATION:** book-alone agg OOS Sharpe = **{b_sr:.2f}** ({'PASS' if valid else 'CHECK'})",
             "", "## Per-window", "",
             "| OOS yr | cross-asset wt | book Sharpe | comb Sharpe | book Calmar | comb Calmar | comb>book? |",
             "|---|---|---|---|---|---|---|"]
    for y, wca, bsr, csr, bc, cc, bmd, cmd in per_window:
        lines.append(f"| {y} | {wca:.0%} | {bsr:.2f} | {csr:.2f} | {bc:.2f} | {cc:.2f} | {'yes' if cc > bc else 'no'} |")
    lines += ["", "## Aggregate + gate verdict", "",
              "| metric | book (ts alone) | combined (Sharpe-prop) |",
              "|---|---|---|",
              f"| Sharpe | {b_sr:.2f} | {c_sr:.2f} |",
              f"| Calmar | {b_cal:.2f} | {c_cal:.2f} |",
              f"| MDD % | {b_mdd:.1f} | {c_mdd:.1f} |",
              f"| corr(C,B) | — | {corr:+.3f} |",
              "",
              "| clause | condition | value | verdict |",
              "|---|---|---|---|",
              f"| M1 Sharpe non-destruction | comb ≥ 0.90×book ({0.90*b_sr:.2f}) | {c_sr:.2f} | {'PASS' if m1 else 'FAIL'} |",
              f"| M2 Calmar improvement | comb ≥ 1.10×book ({1.10*b_cal:.2f}) | {c_cal:.2f} | {'PASS' if m2 else 'FAIL'} |",
              f"| M3 robustness | comb>book Calmar in ≥50% windows | {win_improve}/{n_win} | {'PASS' if m3 else 'FAIL'} |",
              f"| M4 genuine diversifier | corr < 0.70 | {corr:+.3f} | {'PASS' if m4 else 'FAIL'} |",
              "",
              f"### OVERALL: **{'PASS — promotable as a diversifier sleeve at the Sharpe-proportional weight (operator decides)' if passed else 'FAIL — not promotable (v2 is the final authorized weight rule; no further iteration)'}**",
              "",
              "_Weight rule pre-registered in 2026-07-25-portfolio-marginal-gate-v2-SCOPE.md before this run. "
              "Gate clauses/thresholds identical to v1._"]
    out = "ledgers/improvements/2026-07-25-portfolio-marginal-gate-v2-backtest.md"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\n[AGG] book Sharpe={b_sr:.2f} Calmar={b_cal:.2f} MDD={b_mdd:.1f} | "
          f"comb Sharpe={c_sr:.2f} Calmar={c_cal:.2f} MDD={c_mdd:.1f} corr={corr:+.3f}", flush=True)
    print(f"[GATE v2] M1={m1} M2={m2} M3={m3}({win_improve}/{n_win}) M4={m4} -> "
          f"{'PASS' if passed else 'FAIL'} (valid={valid})", flush=True)
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
