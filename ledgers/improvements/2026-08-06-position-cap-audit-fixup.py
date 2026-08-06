"""Handoff B fixup (pre-declared paths only):

(a) connors_rsi2 N=16/N=20 arms were VOID under the prereg exposure control
    (realized invested-gross -29%/-39% rel vs the N=8 arm — sublinear
    occupancy). Per prereg B: fix the scaling ONCE (occupancy-corrected pct
    = design pct x 25.8/realized), re-run, REPLACE the void arms in the
    sweep JSON/md (same trials, measurement correction).
(b) §3.4 verify-don't-assume: dual_ma N=8 vs N=20 full-sim daily max-sector
    share of gross (SEC-SIC map) + max single-name weight + min cash — the
    Hard-Rule direction check, measured not assumed.

Usage: uv run python ledgers/improvements/2026-08-06-position-cap-audit-fixup.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import yaml

from tools.backtest import portfolio_simulator as psim
from tools.quant_strategies import sector_map as sm
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

_spec_path = os.path.join(_ROOT, "ledgers", "improvements",
                          "2026-08-06-position-cap-audit-sweep.py")
_su = importlib.util.spec_from_file_location("cap_sweep", _spec_path)
cap_sweep = importlib.util.module_from_spec(_su)
_su.loader.exec_module(cap_sweep)

OUT_JSON = cap_sweep.OUT_JSON
LIVE = cap_sweep.LIVE

# Occupancy-corrected pct: design pct x (baseline invested gross / arm's).
FIX_ARMS = {16: 0.025 * 25.8 / 18.3, 20: 0.020 * 25.8 / 15.7}


def run_connors_fix() -> list[dict]:
    spec = yaml.safe_load(open(os.path.join(_ROOT, "tools", "quant_strategies",
                                            "connors_rsi2.yml"), encoding="utf-8"))
    benchmark = spec["universe"]["benchmark"]
    tickers = resolve_universe_tickers(spec)
    if benchmark not in tickers:
        tickers.append(benchmark)
    start = date.fromisoformat(str(spec["period"]["start"]))
    end = date.fromisoformat(str(spec["period"]["end"]))
    gate = spec.get("gate", {})
    smin, ddmax, nmin = (float(gate.get("sharpe_min", 1.0)),
                         float(gate.get("max_dd_pct", 25.0)),
                         int(gate.get("n_min", 30)))
    mws = float(gate.get("min_window_sharpe", 0.5))
    mwpr = float(gate.get("min_window_pass_rate", 0.5))
    wf_p = spec.get("walk_forward", {})
    params = cap_sweep.pinned_params(spec, {"entry_threshold": 15, "cooldown_days": 3})
    dfs = _load_universe(tickers, start, end, force_refetch=False)
    signals = cap_sweep.gen_signals(spec, params, dfs, benchmark)

    rows = []
    for n_arm, pct in FIX_ARMS.items():
        cfg = psim.PortfolioConfig(max_positions=n_arm, max_pct_per_position=pct)
        full = psim.simulate(signals, dfs, cfg, sharpe_min=smin,
                             max_dd_pct=ddmax, n_min=nmin, **LIVE)
        wf = psim.simulate_walk_forward(
            signals, dfs, start=start, end=end,
            is_years=int(wf_p.get("is_years", 3)),
            oos_years=int(wf_p.get("oos_years", 1)),
            step_years=int(wf_p.get("step_years", 1)),
            sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin,
            min_window_sharpe=mws, min_window_pass_rate=mwpr,
            config=cfg, **LIVE)
        a = wf.aggregate
        gd = cap_sweep.gross_diagnostics(full, dfs, n_arm)
        windows = [{"is_end_year": sp.in_sample_end.year,
                    "oos_sharpe": r.sharpe_annualised,
                    "clears": bool(r.sharpe_annualised > mws)}
                   for sp, r in wf.window_results]
        row = {
            "setup": "connors_rsi2", "arm_n": n_arm, "pct_per_position": pct,
            "occupancy_corrected": True,
            "full": {"sharpe": full.sharpe_annualised,
                     "mdd": full.max_drawdown_pct, "n": full.n_trades,
                     "n_filled": full.n_filled, "fill": full.fill_rate},
            "oos_agg": {"sharpe": a.sharpe_annualised,
                        "mdd": a.max_drawdown_pct, "n": a.n_trades},
            "per_window": windows,
            "n_windows": wf.n_windows,
            "clears": wf.n_windows_above_floor,
            "window_clause_passed": bool(wf.window_clause_passed),
            "gross": gd,
            "fillable_uncapped": 3338,
            "cap_rejected_est": 3338 - full.n_filled,
        }
        rows.append(row)
        wins = " ".join(f"{w['is_end_year']}:{w['oos_sharpe']:.2f}"
                        f"{'v' if w['clears'] else 'x'}" for w in windows)
        print(f"connors_rsi2 N={n_arm} pct={pct:.4f} (occupancy-corrected): "
              f"FULL S={full.sharpe_annualised:.2f} MDD={full.max_drawdown_pct:.1f} | "
              f"OOS S={a.sharpe_annualised:.2f} MDD={a.max_drawdown_pct:.1f} | "
              f"win {wf.n_windows_above_floor}/{wf.n_windows} [{wins}] | "
              f"gross inv={gd['avg_invested']:.1%} peak={gd['peak']:.1%} "
              f"at-cap={gd['days_at_cap_frac']:.1%}", flush=True)
    return rows


def dual_ma_hard_rule_check() -> dict:
    spec = yaml.safe_load(open(os.path.join(_ROOT, "tools", "quant_strategies",
                                            "dual_ma_trend_following.yml"),
                          encoding="utf-8"))
    benchmark = spec["universe"]["benchmark"]
    tickers = resolve_universe_tickers(spec)
    if benchmark not in tickers:
        tickers.append(benchmark)
    start = date.fromisoformat(str(spec["period"]["start"]))
    end = date.fromisoformat(str(spec["period"]["end"]))
    params = cap_sweep.pinned_params(spec, {"short_period": 20, "long_period": 100})
    dfs = _load_universe(tickers, start, end, force_refetch=False)
    signals = cap_sweep.gen_signals(spec, params, dfs, benchmark)
    smap = sm.build_sector_map([t for t in tickers if t != benchmark])

    out = {}
    for n_arm in (8, 20):
        pct = 0.05 * 8 / n_arm
        cfg = psim.PortfolioConfig(max_positions=n_arm, max_pct_per_position=pct)
        res = psim.simulate(signals, dfs, cfg, **LIVE)
        equity = res.equity_curve
        closes = {}
        for t, df in dfs.items():
            s = df["Close"]
            idx = s.index
            if getattr(idx, "tz", None) is not None:
                s = s.copy()
                s.index = idx.tz_localize(None)
            closes[t] = s.reindex(equity.index, method="ffill")
        events = {}
        for tr in res.trades:
            events.setdefault(tr.fill_date, [[], []])[0].append(tr)
            events.setdefault(tr.exit_date, [[], []])[1].append(tr)
        open_tr = []
        max_name_w = 0.0
        max_sector_w = 0.0
        min_cash_frac = 1.0
        for ts in equity.index:
            ev = events.get(ts.date())
            if ev:
                open_tr = [t for t in open_tr if t not in ev[1]]
                open_tr.extend(ev[0])
            if not open_tr:
                continue
            eq = float(equity.loc[ts])
            if eq <= 0:
                continue
            by_name = {}
            for tr in open_tr:
                px = closes[tr.ticker].loc[ts]
                if px == px:
                    by_name[tr.ticker] = by_name.get(tr.ticker, 0.0) + tr.shares * float(px)
            gross = sum(by_name.values())
            min_cash_frac = min(min_cash_frac, 1.0 - gross / eq)
            if by_name:
                max_name_w = max(max_name_w, max(by_name.values()) / eq)
                by_sec = {}
                for t, v in by_name.items():
                    sec = smap.get(t) or "UNKNOWN"
                    by_sec[sec] = by_sec.get(sec, 0.0) + v
                max_sector_w = max(max_sector_w, max(by_sec.values()) / eq)
        out[n_arm] = {"max_single_name_weight": max_name_w,
                      "max_sector_share_of_equity": max_sector_w,
                      "min_cash_fraction": min_cash_frac}
        print(f"dual_ma N={n_arm}: max name w={max_name_w:.1%} "
              f"max sector share={max_sector_w:.1%} min cash={min_cash_frac:.1%}",
              flush=True)
    return out


def main() -> None:
    fixed = run_connors_fix()
    doc = json.load(open(OUT_JSON, encoding="utf-8"))
    for row in fixed:
        for i, el in enumerate(doc):
            if el["setup"] == row["setup"] and el["arm_n"] == row["arm_n"]:
                doc[i] = row
                break
    assert len(doc) == 12 and sum(1 for el in doc if "sharpe" in el["oos_agg"]) == 12
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    print(f"replaced void arms in {OUT_JSON} (still 12 elements)", flush=True)

    hr = dual_ma_hard_rule_check()
    with open(os.path.join(_ROOT, "ledgers", "improvements",
                           "2026-08-06-position-cap-audit-hardrule.json"),
              "w", encoding="utf-8") as fh:
        json.dump(hr, fh, indent=2)
    print("wrote hard-rule check JSON", flush=True)


if __name__ == "__main__":
    main()
