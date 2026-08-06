"""Handoff B §3.3 — constant-gross cap sweep (SUGGEST-ONLY artifact).

Design FROZEN in ledgers/improvements/2026-08-06-position-cap-audit-prereg.md
(commit f0a3c6d) BEFORE this script produced any number. 12 trials registered
pre-run (trials.yml 91->103).

Arms N in {8,12,16,20} at constant gross: max_positions=N,
max_pct_per_position=0.05*8/N (N x pct = 40% in every arm). Pinned combos:
connors_rsi2 (threshold 15, cooldown 3), dual_ma (20,100),
ts_momentum_liquid_us (lookback 252, top_k=N per arm). Binding psim harness,
LIVE fill config. One uncapped instrumentation run per strategy
(max_positions=4096, pct=0.02) from which ONLY n_signals/n_filled are read.

Usage: uv run python ledgers/improvements/2026-08-06-position-cap-audit-sweep.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import yaml

from tools.backtest import portfolio_simulator as psim
from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

ARMS = [8, 12, 16, 20]
BASE_PCT = 0.05
BASE_N = 8
LIVE = dict(fill_model=psim.FILL_MARKETABLE_LIMIT, momentum_buffer=0.03,
            full_spread_marketable=True)

STRATS = [
    # (setup label, spec file, pinned param overrides, top_k scales with N?)
    ("connors_rsi2", "connors_rsi2.yml",
     {"entry_threshold": 15, "cooldown_days": 3}, False),
    ("dual_ma_trend_following", "dual_ma_trend_following.yml",
     {"short_period": 20, "long_period": 100}, False),
    ("ts_momentum_liquid_us", "ts_momentum_liquid_us.yml",
     {"lookback_days": 252}, True),
]

OUT_MD = os.path.join(_ROOT, "ledgers", "improvements",
                      "2026-08-06-position-cap-audit-sweep.md")
OUT_JSON = os.path.join(_ROOT, "ledgers", "improvements",
                        "2026-08-06-position-cap-audit-sweep.json")


def pinned_params(spec: dict, overrides: dict) -> dict:
    """Spec params with grids collapsed to the pinned combo."""
    params = {}
    for k, v in (spec.get("params") or {}).items():
        if k in overrides:
            params[k] = overrides[k]
        elif isinstance(v, list):
            raise ValueError(f"unpinned grid param {k}={v} — prereg requires a pin")
        else:
            params[k] = v
    return params


def gen_signals(spec: dict, params: dict, dfs: dict, benchmark: str) -> list:
    kind_mod = KIND_REGISTRY[spec["kind"]]
    params = dict(params)
    params.setdefault("benchmark", benchmark)
    state = kind_mod.precompute(dfs, params) if hasattr(kind_mod, "precompute") else None
    signals = []
    for t, df in dfs.items():
        if t == benchmark:
            continue
        signals.extend(kind_mod.replay(df, t, params, state))
    return signals


def gross_diagnostics(res, dfs: dict, cap: int) -> dict:
    """Post-hoc realized gross exposure from the full-period sim."""
    equity = res.equity_curve
    if equity.empty or not res.trades:
        return {"avg_invested": 0.0, "avg_all": 0.0, "peak": 0.0,
                "days_at_cap_frac": 0.0}
    events = {}  # date -> (opens, closes)
    for tr in res.trades:
        events.setdefault(tr.fill_date, [[], []])[0].append(tr)
        events.setdefault(tr.exit_date, [[], []])[1].append(tr)
    open_tr: list = []
    fracs_all, fracs_inv, at_cap = [], [], 0
    closes = {}
    for t, df in dfs.items():
        s = df["Close"]
        idx = s.index
        if getattr(idx, "tz", None) is not None:
            s = s.copy()
            s.index = idx.tz_localize(None)
        closes[t] = s.reindex(equity.index, method="ffill")
    for ts in equity.index:
        d = ts.date()
        ev = events.get(d)
        if ev:
            open_tr = [t for t in open_tr if t not in ev[1]]
            open_tr.extend(ev[0])
        gross = 0.0
        for tr in open_tr:
            s = closes.get(tr.ticker)
            if s is None:
                continue
            px = s.loc[ts]
            if px == px:  # not NaN
                gross += tr.shares * float(px)
        eq = float(equity.loc[ts])
        frac = gross / eq if eq > 0 else 0.0
        fracs_all.append(frac)
        if open_tr:
            fracs_inv.append(frac)
            if len(open_tr) >= cap:
                at_cap += 1
    return {
        "avg_invested": sum(fracs_inv) / len(fracs_inv) if fracs_inv else 0.0,
        "avg_all": sum(fracs_all) / len(fracs_all) if fracs_all else 0.0,
        "peak": max(fracs_all) if fracs_all else 0.0,
        "days_at_cap_frac": at_cap / len(fracs_all) if fracs_all else 0.0,
    }


def main() -> None:
    results = []
    md = ["# Handoff B §3.3 — constant-gross cap sweep (2026-08-06)", "",
          "Design frozen pre-run: `2026-08-06-position-cap-audit-prereg.md` "
          "(commit f0a3c6d). Binding psim net-of-cost harness, LIVE fills. "
          "Constant gross: N x pct = 40% every arm.", ""]
    for setup, spec_file, overrides, scale_topk in STRATS:
        spec = yaml.safe_load(open(os.path.join(_ROOT, "tools", "quant_strategies",
                                                spec_file), encoding="utf-8"))
        benchmark = spec["universe"]["benchmark"]
        tickers = resolve_universe_tickers(spec)
        if benchmark not in tickers:
            tickers.append(benchmark)
        start = date.fromisoformat(str(spec["period"]["start"]))
        end = date.fromisoformat(str(spec["period"]["end"]))
        gate = spec.get("gate", {})
        smin = float(gate.get("sharpe_min", 1.0))
        ddmax = float(gate.get("max_dd_pct", 25.0))
        nmin = int(gate.get("n_min", 30))
        mws = float(gate.get("min_window_sharpe", 0.5))
        mwpr = float(gate.get("min_window_pass_rate", 0.5))
        wf_p = spec.get("walk_forward", {})
        is_y, oos_y, step_y = (int(wf_p.get("is_years", 3)),
                               int(wf_p.get("oos_years", 1)),
                               int(wf_p.get("step_years", 1)))
        params = pinned_params(spec, overrides)

        print(f"\n=== {setup}: loading {len(tickers)} tickers ...", flush=True)
        dfs = _load_universe(tickers, start, end, force_refetch=False)

        # Uncapped instrumentation (prereg: ONLY n_signals / n_filled read).
        p_u = dict(params)
        if scale_topk:
            p_u["top_k"] = max(ARMS)
        sig_u = gen_signals(spec, p_u, dfs, benchmark)
        res_u = psim.simulate(sig_u, dfs,
                              psim.PortfolioConfig(max_positions=4096,
                                                   max_pct_per_position=0.02),
                              sharpe_min=smin, max_dd_pct=ddmax, n_min=nmin,
                              **LIVE)
        fillable = res_u.n_filled
        print(f"{setup}: uncapped instrumentation n_signals={res_u.n_signals} "
              f"price-fillable={fillable} (Sharpe deliberately not read)", flush=True)

        base_signals = None if scale_topk else gen_signals(spec, params, dfs, benchmark)
        strat_rows = []
        for n_arm in ARMS:
            pct = BASE_PCT * BASE_N / n_arm
            cfg = psim.PortfolioConfig(max_positions=n_arm, max_pct_per_position=pct)
            if scale_topk:
                p_a = dict(params); p_a["top_k"] = n_arm
                signals = gen_signals(spec, p_a, dfs, benchmark)
            else:
                signals = base_signals
            full = psim.simulate(signals, dfs, cfg, sharpe_min=smin,
                                 max_dd_pct=ddmax, n_min=nmin, **LIVE)
            wf = psim.simulate_walk_forward(
                signals, dfs, start=start, end=end, is_years=is_y,
                oos_years=oos_y, step_years=step_y, sharpe_min=smin,
                max_dd_pct=ddmax, n_min=nmin, min_window_sharpe=mws,
                min_window_pass_rate=mwpr, config=cfg, **LIVE)
            a = wf.aggregate
            gd = gross_diagnostics(full, dfs, n_arm)
            cap_rej = (fillable - full.n_filled) if (not scale_topk or n_arm == max(ARMS)) else None
            windows = [{"is_end_year": sp.in_sample_end.year,
                        "oos_sharpe": r.sharpe_annualised,
                        "clears": bool(r.sharpe_annualised > mws)}
                       for sp, r in wf.window_results]
            row = {
                "setup": setup, "arm_n": n_arm, "pct_per_position": pct,
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
                "fillable_uncapped": fillable,
                "cap_rejected_est": cap_rej,
            }
            strat_rows.append(row)
            results.append(row)
            wins = " ".join(f"{w['is_end_year']}:{w['oos_sharpe']:.2f}"
                            f"{'v' if w['clears'] else 'x'}" for w in windows)
            print(f"{setup} N={n_arm:2d} pct={pct:.4f}: "
                  f"FULL S={full.sharpe_annualised:.2f} MDD={full.max_drawdown_pct:.1f} "
                  f"n={full.n_trades} | OOS S={a.sharpe_annualised:.2f} "
                  f"MDD={a.max_drawdown_pct:.1f} | win {wf.n_windows_above_floor}/"
                  f"{wf.n_windows} [{wins}] | gross inv={gd['avg_invested']:.1%} "
                  f"peak={gd['peak']:.1%} at-cap={gd['days_at_cap_frac']:.1%} | "
                  f"cap_rej={cap_rej}", flush=True)

        md += [f"## {setup} (pinned {overrides})", "",
               "| N | pct | OOS-agg S | OOS MDD% | OOS n | windows | FULL S | "
               "gross(inv) | gross(peak) | at-cap % | cap-rejected (est) |",
               "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in strat_rows:
            g = r["gross"]
            md.append(
                f"| {r['arm_n']} | {r['pct_per_position']:.4f} | "
                f"{r['oos_agg']['sharpe']:.2f} | {abs(r['oos_agg']['mdd']):.1f} | "
                f"{r['oos_agg']['n']} | {r['clears']}/{r['n_windows']} | "
                f"{r['full']['sharpe']:.2f} | {g['avg_invested']:.1%} | "
                f"{g['peak']:.1%} | {g['days_at_cap_frac']:.1%} | "
                f"{r['cap_rejected_est']} |")
        md.append("")

    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    with open(OUT_MD, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    print(f"\nwrote {OUT_MD}\nwrote {OUT_JSON}", flush=True)


if __name__ == "__main__":
    main()
