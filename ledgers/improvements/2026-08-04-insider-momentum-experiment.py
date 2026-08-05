"""INSIDER x MOMENTUM — momentum-rank selection on the insider-buying KIND.
Claude1 paper-research pilot, 2026-08-04. Suggest-only research.

The insider KIND was RETIRED (net Sharpe 0.14) with a diagnosis of capacity+selection,
NOT signal death: the 8-cap rejected 372/489 outcomes and the adverse-selection check
showed FILLED names +7.7% fwd vs MISSED +40.3% — first-come selection threw away the
best names. Best-first BY CONVICTION was tried (0.14 -> 0.67, helped, still <1.0) and
the conviction layer is degraded (non-PIT shares, mostly UNRATED). Untested flank:
best-first BY MOMENTUM — the exact selection fix that made ts_momentum deployable.
Momentum needs only price data (no degraded inputs), and "insider buys + market
already agrees" is the Cohen-Malloy-style interaction the literature supports.

Arms (ONE identical accounting loop, copied from the committed best-first experiment,
which itself mirrors portfolio_simulator.simulate — only selection/filter varies):
  A  FIRST-COME baseline          (validation: must reproduce the ~0.14 RETIRE verdict)
  B  MOM-GATE first-come          (drop signals with trailing 126d momentum <= 0)
  C  MOM-BEST-FIRST W=10          (freed slot -> highest-momentum signal in backlog)
  D  MOM-BEST-FIRST W=21
  E  MOM-GATE + MOM-BEST-FIRST W=21
  F  E + shorter hold 63d         (capacity relief: 2x slot turnover)

Net-of-cost, same gate as the retirement: Sharpe>1.0 & |MDD|<25% & n>=30. DSR reported
on the best arm's equity curve with n_trials=12 (6 arms here + ~6 prior insider trials
in the registry). Trials note: on any merge these 6 arms belong in ledgers/trials.yml.

Usage:  uv run python ledgers/improvements/2026-08-04-insider-momentum-experiment.py
"""
from __future__ import annotations
import os, sys, math
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from datetime import date
import pandas as pd
import yaml
from tools.backtest import security_master, cost_model, sharpe_stats as ss
from tools.backtest.portfolio_simulator import (
    PortfolioConfig, _to_date_index, _close_on_or_before, _row_on_or_after,
    _compute_fill, _equity_metrics)
from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe
from tools.auto_paper import entry_pricing

MOM_LB = 126           # trailing momentum lookback (bars), measured to the bar BEFORE fill
N_TRIALS_DSR = 12      # 6 arms here + ~6 prior insider-KIND trials (gate/gap/sleeve/bestfirstx3)

spec = yaml.safe_load(open(os.path.join(_ROOT, "tools/quant_strategies/event_insider_buying.yml"), encoding="utf-8"))
km = KIND_REGISTRY[spec["kind"]]
base_params = dict(spec.get("params", {}))
bench = spec["universe"]["benchmark"]
base_params.setdefault("benchmark", bench)
tickers = resolve_universe_tickers(spec)
if bench not in tickers:
    tickers.append(bench)
start = date.fromisoformat(str(spec["period"]["start"]))
end = date.fromisoformat(str(spec["period"]["end"]))
g = spec.get("gate", {})
SMIN, DDMAX, NMIN = float(g.get("sharpe_min", 1.0)), float(g.get("max_dd_pct", 25.0)), int(g.get("n_min", 30))

print(f"loading universe ({len(tickers)} tickers) ...", flush=True)
dfs_raw = _load_universe(tickers, start, end, force_refetch=False)
dfs = {t: _to_date_index(df) for t, df in dfs_raw.items()}
state = km.precompute(dfs_raw, base_params)


def build_signals(max_hold_days=None):
    p = dict(base_params)
    if max_hold_days is not None:
        p["max_hold_days"] = max_hold_days
    out = []
    for t, df in dfs_raw.items():
        if t != bench:
            out.extend(km.replay(df, t, p, state))
    return out


def mom_at(ticker, ts):
    """Trailing MOM_LB-bar return ending at the bar BEFORE ts. None if insufficient history."""
    df = dfs.get(ticker)
    if df is None:
        return None
    pos = df.index.searchsorted(ts)
    i1 = pos - 1
    i0 = i1 - MOM_LB
    if i0 < 0 or i1 < 0:
        return None
    c1 = float(df.iloc[i1]["Close"]); c0 = float(df.iloc[i0]["Close"])
    if c0 <= 0 or pd.isna(c0) or pd.isna(c1):
        return None
    return c1 / c0 - 1.0


def simulate(signals, *, best_first=False, W=0, mom_gate=False):
    cfg = PortfolioConfig()
    all_idx = sorted({ts for df in dfs.values() for ts in df.index})
    cal_pos = {ts: i for i, ts in enumerate(all_idx)}
    pend = {}
    n_sig = 0
    n_gated = 0
    for sig in signals:
        df = dfs.get(sig.ticker)
        if df is None:
            continue
        pivot = _close_on_or_before(df, sig.entry_date)
        if pivot is None or pivot <= 0:
            continue
        hit = _row_on_or_after(df, sig.fill_date)
        if hit is None:
            continue
        fts, _ = hit
        m = mom_at(sig.ticker, fts)
        if mom_gate and (m is None or m <= 0.0):
            n_gated += 1
            continue
        kind = entry_pricing.resolve_kind(sig.setup_type)
        pend.setdefault(fts, []).append((sig, kind, pivot, cal_pos[fts], (m if m is not None else -9.9)))
        n_sig += 1
    cash = cfg.starting_equity
    opens = []
    eq = []
    n_filled = 0
    filled_mom = []
    unfilled_mom = []
    backlog = []

    def open_pos(sig, kind, fill_price, ts, d, df, stop, target):
        nonlocal cash, n_filled
        adv = security_master.dollar_adv(df, d, window=cfg.adv_window)
        lf = cfg.min_liquidity_factor
        if adv is not None and cfg.ref_adv_full_weight > 0:
            lf = max(cfg.min_liquidity_factor, min(1.0, adv / cfg.ref_adv_full_weight))
        eqnow = cash + sum(p['shares'] * (_close_on_or_before(dfs[p['t']], d) or p['efp']) for p in opens)
        tgt_d = min(cfg.max_pct_per_position * lf * eqnow, cash)
        half = security_master.liquidity_tier(adv).half_spread_bps
        bps = cost_model.one_side_cost_bps(tgt_d, adv, half) if cfg.apply_costs else 0.0
        nb = cost_model.apply_buy_cost(fill_price, bps)
        sh = int(tgt_d // nb)
        if sh <= 0:
            return False
        cash -= sh * nb
        opens.append(dict(sig=sig, t=sig.ticker, kind=kind, shares=sh, efp=fill_price,
                          enp=nb, fts=ts, stop=stop, target=target))
        n_filled += 1
        return True

    for ts in all_idx:
        d = ts.date()
        still = []
        for p in opens:
            df = dfs[p['t']]
            if ts not in df.index:
                still.append(p)
                continue
            bar = df.loc[ts]
            o, h, l, c = float(bar['Open']), float(bar['High']), float(bar['Low']), float(bar['Close'])
            bh = int(df.index.searchsorted(ts) - df.index.searchsorted(p['fts']))
            ep = None
            if o <= p['stop']:
                ep = o
            elif l <= p['stop']:
                ep = p['stop']
            elif p['target'] is not None and h >= p['target']:
                ep = p['target']
            elif bh >= p['sig'].max_hold_days:
                ep = c
            if ep is None:
                still.append(p)
                continue
            adv = security_master.dollar_adv(df, d, window=cfg.adv_window)
            half = security_master.liquidity_tier(adv).half_spread_bps
            bps = cost_model.one_side_cost_bps(p['shares'] * ep, adv, half) if cfg.apply_costs else 0.0
            cash += p['shares'] * cost_model.apply_sell_cost(ep, bps)
        opens = still

        if not best_first:
            for sig, kind, pivot, _fi, m in pend.get(ts, []):
                fb = dfs[sig.ticker].loc[ts]
                fp = _compute_fill(kind, pivot, fb)
                if fp is None:
                    unfilled_mom.append(m)
                    continue
                if len(opens) >= cfg.max_positions:
                    unfilled_mom.append(m)
                    continue
                if open_pos(sig, kind, fp, ts, d, dfs[sig.ticker], sig.stop_price, sig.target_price):
                    filled_mom.append(m)
        else:
            backlog.extend(pend.get(ts, []))
            keep = []
            for item in backlog:
                if cal_pos[ts] - item[3] > W:
                    unfilled_mom.append(item[4])
                else:
                    keep.append(item)
            backlog = sorted(keep, key=lambda it: it[4], reverse=True)   # momentum-rank
            rest = []
            for sig, kind, _pv, fi, m in backlog:
                if len(opens) >= cfg.max_positions:
                    rest.append((sig, kind, _pv, fi, m))
                    continue
                df = dfs[sig.ticker]
                if ts not in df.index:
                    rest.append((sig, kind, _pv, fi, m))
                    continue
                pos = df.index.get_loc(ts)
                if pos < 1:
                    rest.append((sig, kind, _pv, fi, m))
                    continue
                prev_close = float(df.iloc[pos - 1]['Close'])
                fp = _compute_fill(kind, prev_close, df.loc[ts])
                if fp is None:
                    rest.append((sig, kind, _pv, fi, m))
                    continue
                dist = sig.entry_price - sig.stop_price
                stop = fp - dist
                target = (fp + (sig.target_price - sig.entry_price)) if sig.target_price else None
                if open_pos(sig, kind, fp, ts, d, df, stop, target):
                    filled_mom.append(m)
                else:
                    rest.append((sig, kind, _pv, fi, m))
            backlog = rest

        mtm = cash + sum(p['shares'] * float(dfs[p['t']].loc[ts]['Close']) for p in opens if ts in dfs[p['t']].index)
        eq.append((ts, mtm))

    if best_first:
        for item in backlog:
            unfilled_mom.append(item[4])
    equity = pd.Series([v for _, v in eq], index=pd.DatetimeIndex([t for t, _ in eq]))
    sharpe, mdd = _equity_metrics(equity)
    ret = (float(equity.iloc[-1]) / float(equity.iloc[0]) - 1.0) if len(equity) else 0.0
    return dict(sharpe=sharpe, mdd=mdd, n=n_filled, fill=n_filled / n_sig if n_sig else 0,
                ret=ret, gate=(sharpe > SMIN and abs(mdd) < DDMAX and n_filled >= NMIN),
                fm=filled_mom, um=unfilled_mom, n_sig=n_sig, n_gated=n_gated, equity=equity)


def dsr_of(equity):
    r = equity.pct_change().dropna()
    if len(r) < 30:
        return float("nan")
    m = ss.sharpe_moments(list(r.values))
    return float(ss.deflated_sharpe(m["sr"], int(m["n"]), m["skew"], m["kurt_raw"],
                                    var_trials=ss.annualized_to_per_period(0.5) ** 2,
                                    n_trials=N_TRIALS_DSR)["dsr"])


def row(label, r):
    return (f"| {label} | {r['sharpe']:.2f} | {abs(r['mdd']):.1f} | {r['n']} | {r['fill']*100:.0f} | "
            f"{r['ret']*100:.1f} | {'PASS' if r['gate'] else 'FAIL'} |")


def main():
    sig126 = build_signals()
    sig63 = build_signals(max_hold_days=63)
    print(f"{len(sig126)} signals (126d hold) / {len(sig63)} (63d hold)", flush=True)
    arms = {}
    arms["A FIRST-COME baseline"] = simulate(sig126)
    print("A done", flush=True)
    arms["B MOM-GATE first-come"] = simulate(sig126, mom_gate=True)
    print("B done", flush=True)
    arms["C MOM-BEST-FIRST W=10"] = simulate(sig126, best_first=True, W=10)
    print("C done", flush=True)
    arms["D MOM-BEST-FIRST W=21"] = simulate(sig126, best_first=True, W=21)
    print("D done", flush=True)
    arms["E MOM-GATE + BEST-FIRST W=21"] = simulate(sig126, best_first=True, W=21, mom_gate=True)
    print("E done", flush=True)
    arms["F = E + 63d hold"] = simulate(sig63, best_first=True, W=21, mom_gate=True)
    print("F done", flush=True)

    best_key = max(arms, key=lambda k: arms[k]["sharpe"])
    d_best = dsr_of(arms[best_key]["equity"])
    lines = ["# Insider x momentum — momentum-rank selection experiment (pilot, 2026-08-04)", "",
             "Retired insider KIND re-attacked at its diagnosed bottleneck (selection/capacity, not signal). "
             f"Identical accounting loop as the committed best-first experiment; only selection varies. "
             f"Momentum = trailing {MOM_LB}d return to the bar before fill (no lookahead). Net-of-cost; "
             f"gate Sharpe>{SMIN} & |MDD|<{DDMAX}% & n>={NMIN}.", "",
             "| arm | Sharpe | |MDD|% | n | fill% | ret% | gate |", "|---|---|---|---|---|---|---|"]
    for k, r in arms.items():
        lines.append(row(k, r))
    gm = arms["B MOM-GATE first-come"]["n_gated"]
    lines += ["", f"- MOM-GATE dropped {gm} of {gm + arms['B MOM-GATE first-come']['n_sig']} signals (momentum<=0).",
              f"- Best arm: **{best_key}** — DSR **{d_best:.2f}** (n_trials={N_TRIALS_DSR}; needs >0.95).",
              f"- Prior reference points: first-come RETIRE 0.14 · conviction-best-first 0.67.", "",
              "## Verdict: **" + ("BEST ARM CLEARS THE GATE -> blind judge+critic next"
                                  if arms[best_key]["gate"] and d_best > 0.95 else
                                  "no arm clears the full gate (incl. DSR)") + "**",
              "- Trials-registry note: these 6 arms belong in ledgers/trials.yml on any merge.",
              "- Suggest-only; nothing applied."]
    out = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-04-insider-momentum-experiment.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print()
    for k, r in arms.items():
        print(f"{k:30} Sharpe={r['sharpe']:>5.2f} |MDD|={abs(r['mdd']):>5.1f}% n={r['n']:>3} "
              f"fill={r['fill']*100:>3.0f}% ret={r['ret']*100:>6.1f}% -> {'PASS' if r['gate'] else 'FAIL'}")
    print(f"\nbest={best_key} DSR={d_best:.2f}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
