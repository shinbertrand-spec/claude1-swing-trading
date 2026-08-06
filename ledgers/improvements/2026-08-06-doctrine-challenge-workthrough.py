"""Doctrine-review challenge work-through — distribution stats + ts_momentum miss-rate.
Claude1, 2026-08-06. Measurement only (no gate arithmetic, no trials implications).

Challenge 1: is the GO-NARROW conclusion robust to (a) the slippage assumption,
(b) mean vs MEDIAN cohort comparison, (c) statistical noise? -> per-cohort std,
t-stat of the missed-vs-filled difference, win rates, median ratios.

Challenge 2: does the redirection to ts_momentum hold water? -> measure ts_momentum's
own price-miss fraction under the live +3% marketable cap, and the pivot-vs-open
forward-return spread of its missed entries (sizes the R1/R2 benefit of LOO).

Usage:  uv run python ledgers/improvements/2026-08-06-doctrine-challenge-workthrough.py
"""
from __future__ import annotations
import os, sys, math
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from datetime import date
import numpy as np
import yaml

from tools.backtest.portfolio_simulator import (
    _to_date_index, _close_on_or_before, _row_on_or_after, _compute_fill,
    FILL_MARKETABLE_LIMIT)
from tools.auto_paper import entry_pricing
from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

MOMENTUM_BUFFER = 0.03
SLIP = 0.0020


def fwd_from(df, fill_pos, H, ref):
    if ref is None or ref <= 0:
        return None
    end = min(fill_pos + H, len(df) - 1)
    return float(df.iloc[end]["Close"]) / float(ref) - 1.0


def welch_t(a, b):
    a, b = np.asarray(a), np.asarray(b)
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    return float((a.mean() - b.mean()) / math.sqrt(va + vb)) if (va + vb) > 0 else 0.0


def cohort(spec_name, overrides, H_override=None):
    spec = yaml.safe_load(open(os.path.join(_ROOT, f"tools/quant_strategies/{spec_name}.yml"), encoding="utf-8"))
    bench = spec["universe"]["benchmark"]
    params = {k: (v if not isinstance(v, list) else v[-1]) for k, v in spec.get("params", {}).items()}
    params.update(overrides)
    params.setdefault("benchmark", bench)
    tickers = resolve_universe_tickers(spec)
    if bench not in tickers:
        tickers.append(bench)
    start = date.fromisoformat(str(spec["period"]["start"]))
    end = date.fromisoformat(str(spec["period"]["end"]))
    dfs_raw = _load_universe(tickers, start, end, force_refetch=False)
    dfs = {t: _to_date_index(df) for t, df in dfs_raw.items()}
    km = KIND_REGISTRY[spec["kind"]]
    state = km.precompute(dfs_raw, params)
    signals = []
    for t, df in dfs_raw.items():
        if t != bench:
            signals.extend(km.replay(df, t, params, state))
    kind = entry_pricing.resolve_kind(spec["kind"])
    H = H_override or int(params.get("max_hold_days", 21))
    filled, missed_o20 = [], []
    gaps = []
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
        pos = int(df.index.get_loc(fts))
        bar = df.loc[fts]
        opn = float(bar["Open"])
        if opn <= 0 or not np.isfinite(opn):
            continue
        fp = _compute_fill(kind, pivot, bar, fill_model=FILL_MARKETABLE_LIMIT, momentum_buffer=MOMENTUM_BUFFER)
        if fp is None:
            r = fwd_from(df, pos, H, opn * (1 + SLIP))
            if r is not None:
                missed_o20.append(r); gaps.append(opn / pivot - 1.0)
        else:
            r = fwd_from(df, pos, H, fp)
            if r is not None:
                filled.append(r)
    return np.array(filled), np.array(missed_o20), np.array(gaps), len(signals)


def report(label, f, m, g, n_sig):
    t = welch_t(m, f)
    print(f"\n=== {label} ===")
    print(f"signals {n_sig} -> filled {len(f)} / price-missed {len(m)} ({len(m)/max(len(f)+len(m),1)*100:.1f}%)")
    print(f"FILLED : mean {f.mean()*100:+.2f}% med {np.median(f)*100:+.2f}% sd {f.std(ddof=1)*100:.1f}% win {np.mean(f>0)*100:.0f}%")
    print(f"MISSED (open+20bps): mean {m.mean()*100:+.2f}% med {np.median(m)*100:+.2f}% sd {m.std(ddof=1)*100:.1f}% win {np.mean(m>0)*100:.0f}%")
    print(f"mean ratio {m.mean()/f.mean():.2f}x · MEDIAN ratio {np.median(m)/np.median(f):.2f}x · Welch t(diff) {t:.2f}")
    print(f"missed gap paid at open: mean {g.mean()*100:+.2f}%")
    return dict(mean_ratio=m.mean()/f.mean(), med_ratio=np.median(m)/np.median(f), t=t)


def main():
    print("cohort 1: event_earnings_drift rank1 ...", flush=True)
    f, m, g, n = cohort("event_earnings_drift", {"ear_top_pct": 20, "history_condition": False, "max_hold_days": 21})
    report("event_earnings_drift rank1 (H=21)", f, m, g, n)

    print("\ncohort 2: event_insider_buying ...", flush=True)
    f2, m2, g2, n2 = cohort("event_insider_buying", {})
    report("event_insider_buying (H=126)", f2, m2, g2, n2)

    print("\ncohort 3: ts_momentum_liquid_us deployed (challenge 2 sizing) ...", flush=True)
    f3, m3, g3, n3 = cohort("ts_momentum_liquid_us", {"lookback_days": 252}, H_override=21)
    report("ts_momentum_liquid_us (lookback 252, H=21)", f3, m3, g3, n3)


if __name__ == "__main__":
    main()
