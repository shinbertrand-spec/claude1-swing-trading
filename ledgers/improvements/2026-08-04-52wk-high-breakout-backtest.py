"""Strategy #2 — 52-WEEK-HIGH BREAKOUT (Donchian / George-Hwang), suggest-only research.
Claude1 paper-research pilot, 2026-08-04.

Hypothesis: stocks making new 52-week highs continue to outperform (George-Hwang 2004;
Donchian channel breakout). Distinct from ts_momentum (which ranks trailing return) —
here the signal is a fresh N-day-high breakout, entered on the breakout, ATR-stopped,
time-capped, 8-position cap.

Routed through the SAME net-of-cost walk-forward gate the live setups use
(tools.backtest.portfolio_simulator.simulate_walk_forward): marketable-limit fill +
FULL effective spread + OHLC fills + 8-cap, rolling 3y-IS/1y-OOS/1y-step (6 OOS windows).
Gate: agg Sharpe>1.0 & |MDD|<25% & n>=30 AND >=50% of OOS windows clear Sharpe>0.5,
plus Deflated Sharpe>0.95 on the aggregate OOS curve. ALL NET OF COST.

Reads cached bars + repo simulator/universe. Writes ONLY its own report under
ledgers/improvements/. No strategy-code / deployable-set / order writes.

Usage:  uv run python ledgers/improvements/2026-08-04-52wk-high-breakout-backtest.py
"""
from __future__ import annotations
import os, sys, math
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from datetime import date
import numpy as np
import pandas as pd
from tools.backtest import portfolio_simulator as psim, sharpe_stats as ss
from tools.backtest.setup_replay import TradeSignal
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

KIND = "high_breakout_52w"
FILL_KIND = "ts_momentum"   # fill-routing only: a breakout is a chase-up marketable entry
                            # (MOMENTUM_KINDS). Strategy identity stays high_breakout_52w in the report.
SPEC = {"universe": {"name": "sp500_leaning_88", "benchmark": "SPY"}}
START, END = date(2017, 1, 1), date(2026, 5, 25)
PARAMS = dict(lookback_high=252, cooldown_days=21, atr_period=20,
              atr_stop_multiple=3.0, max_hold_days=60)
GATE = dict(sharpe_min=1.0, max_dd_pct=25.0, n_min=30, min_window_sharpe=0.5, min_window_pass_rate=0.5)


def _atr(df: pd.DataFrame, i: int, period: int) -> float | None:
    if i < period: return None
    h = df["High"].values; l = df["Low"].values; c = df["Close"].values
    trs = []
    for k in range(i - period + 1, i + 1):
        tr = max(h[k] - l[k], abs(h[k] - c[k - 1]), abs(l[k] - c[k - 1]))
        trs.append(tr)
    return float(np.mean(trs)) if trs else None


def breakout_replay(df: pd.DataFrame, ticker: str, params: dict) -> list[TradeSignal]:
    if ticker == SPEC["universe"]["benchmark"]:
        return []
    lb = int(params["lookback_high"]); cd = int(params["cooldown_days"])
    atrp = int(params["atr_period"]); mult = float(params["atr_stop_multiple"])
    mh = int(params["max_hold_days"])
    close = df["Close"].values; idx = list(df.index)
    sigs: list[TradeSignal] = []
    last_sig = -10**9
    for i in range(lb, len(df) - 1):
        prior_high = close[i - lb:i].max()          # highest close over the prior lb days
        if not (close[i] > prior_high):              # fresh N-day-high breakout on bar i
            continue
        if i - last_sig < cd:                        # cooldown: one signal per run
            continue
        atr = _atr(df, i, atrp)
        if atr is None or atr <= 0:
            continue
        nxt = df.iloc[i + 1]
        entry = float(nxt["Open"])
        if entry <= 0 or pd.isna(entry):
            continue
        stop = entry - mult * atr
        if stop >= entry:
            continue
        last_sig = i
        sigs.append(TradeSignal(
            ticker=ticker, setup_type=FILL_KIND, setup_grade="B",
            entry_date=pd.Timestamp(idx[i]).date(), fill_date=pd.Timestamp(idx[i + 1]).date(),
            entry_price=entry, stop_price=stop, target_price=None,
            max_hold_days=mh, atr_at_signal=float(atr), notes={}))
    return sigs


def dsr_from_curve(res) -> float:
    ec = getattr(res, "equity_curve", None)
    if ec is None or len(ec) < 30: return float("nan")
    r = pd.Series(ec).pct_change().dropna()
    m = ss.sharpe_moments(list(r.values))
    var_trials = ss.annualized_to_per_period(0.5) ** 2      # conservative spread of trial Sharpes
    out = ss.deflated_sharpe(m["sr"], int(m["n"]), m["skew"], m["kurt_raw"],
                             var_trials=var_trials, n_trials=8)   # ~8 strategy-variants explored in the pilot
    return float(out["dsr"])


def main() -> None:
    tickers = resolve_universe_tickers(SPEC)
    bench = SPEC["universe"]["benchmark"]
    if bench not in tickers: tickers.append(bench)
    print(f"loading {len(tickers)} tickers ...", flush=True)
    dfs = _load_universe(tickers, START, END, force_refetch=False)

    signals = []
    for t, df in dfs.items():
        signals.extend(breakout_replay(df, t, PARAMS))
    print(f"{len(signals)} breakout signals", flush=True)

    LIVE = dict(fill_model=psim.FILL_MARKETABLE_LIMIT, momentum_buffer=0.03, full_spread_marketable=True)
    wf = psim.simulate_walk_forward(
        signals, dfs, start=START, end=END, is_years=3, oos_years=1, step_years=1,
        sharpe_min=GATE["sharpe_min"], max_dd_pct=GATE["max_dd_pct"], n_min=GATE["n_min"],
        min_window_sharpe=GATE["min_window_sharpe"], min_window_pass_rate=GATE["min_window_pass_rate"],
        **LIVE)
    a = wf.aggregate
    d = dsr_from_curve(a)
    win = " ".join(f"{sp.in_sample_end.year}:{r.sharpe_annualised:.2f}"
                   f"{'v' if r.sharpe_annualised>GATE['min_window_sharpe'] else 'x'}"
                   for sp, r in wf.window_results)
    gate_pass = bool(wf.aggregate_gate_passed and wf.window_clause_passed and d > 0.95)

    lines = [f"# Strategy #2 — 52-week-high breakout — net-of-cost walk-forward gate (pilot, 2026-08-04)", "",
             f"Universe sp500_leaning_88 (+SPY). {START}..{END}. Params {PARAMS}.",
             f"Net-of-cost: marketable-limit + full spread + OHLC fills + 8-cap; rolling 3y-IS/1y-OOS/1y-step.",
             "",
             f"- Signals: **{len(signals)}**",
             f"- Aggregate OOS: Sharpe **{a.sharpe_annualised:.2f}** · |MDD| {abs(a.max_drawdown_pct):.1f}% · n {a.n_trades} · fill {a.fill_rate*100:.0f}%",
             f"- Aggregate gate: **{'PASS' if wf.aggregate_gate_passed else 'FAIL'}**",
             f"- Per-window: {wf.n_windows_above_floor}/{wf.n_windows} clear Sharpe>0.5 (rate {wf.window_pass_rate:.0%}) -> **{'PASS' if wf.window_clause_passed else 'FAIL'}**",
             f"  - windows: {win}",
             f"- Deflated Sharpe (agg OOS curve): **{d:.2f}** (gate >0.95)",
             "",
             f"## Verdict: **{'DEPLOY-CANDIDATE (clears gate)' if gate_pass else 'FAIL — does not clear the net-of-cost gate'}**",
             "- Suggest-only; nothing applied. If PASS, broaden to liquid_us_2026q2 + run blind judge/critic before any operator merge."]
    out = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-04-52wk-high-breakout-backtest.md")
    with open(out, "w", encoding="utf-8") as fh: fh.write("\n".join(lines) + "\n")
    print(f"AGG OOS Sharpe={a.sharpe_annualised:.2f} |MDD|={abs(a.max_drawdown_pct):.1f}% n={a.n_trades} "
          f"per-window {wf.n_windows_above_floor}/{wf.n_windows} DSR={d:.2f} -> {'PASS' if gate_pass else 'FAIL'}")
    print("windows:", win)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
