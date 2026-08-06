"""Task 0 — gap-adjusted missed-cohort measurement (same-session entry doctrine review).
Claude1, 2026-08-06. Suggest-only measurement; NOT a backtest / re-test:
no simulator, no equity curve, no gate arithmetic. See the pre-registered stopping
rule in 2026-08-06-same-session-entry-doctrine-review.md (written before this ran).

For each retired event KIND's signal cohort (regenerated from its frozen events file +
retired spec params), split filled/missed under the EXACT live rule
(_compute_fill, FILL_MARKETABLE_LIMIT, +3% momentum buffer, pivot = close<=entry_date),
then measure unconditional H-bar forward returns from four references:
  pivot          — the verdicts' pre-gap number (replication check)
  open           — entry-session actual opening print (achievable via MOO)
  open+20bps     — realistic opening slippage        <- the pre-registered comparator
  open+50bps     — stress
Filled cohort measured from its modeled fill (open) over the same H.

Usage:  uv run python ledgers/improvements/2026-08-06-same-session-entry-task0.py
"""
from __future__ import annotations
import os, sys
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
SLIPS = [0.0, 0.0020, 0.0050]      # 0 / 20bps / 50bps on the open

COHORTS = [
    {"label": "event_earnings_drift rank1 (ear20/off/21)", "spec": "event_earnings_drift",
     "overrides": {"ear_top_pct": 20, "history_condition": False, "max_hold_days": 21}},
    {"label": "event_insider_buying (retired params)", "spec": "event_insider_buying",
     "overrides": {}},
]


def stats(x):
    x = np.asarray([v for v in x if v is not None and np.isfinite(v)])
    if len(x) == 0:
        return dict(n=0, mean=0.0, med=0.0)
    return dict(n=len(x), mean=float(np.mean(x)) * 100, med=float(np.median(x)) * 100)


def fwd_from(df, fill_pos, H, ref_price):
    """Unconditional H-bar forward return from ref_price (close at fill_pos+H or last)."""
    if ref_price is None or ref_price <= 0:
        return None
    end = min(fill_pos + H, len(df) - 1)
    if end <= fill_pos and end != fill_pos:
        return None
    return float(df.iloc[end]["Close"]) / float(ref_price) - 1.0


def run_cohort(c, out_lines):
    spec = yaml.safe_load(open(os.path.join(_ROOT, f"tools/quant_strategies/{c['spec']}.yml"), encoding="utf-8"))
    bench = spec["universe"]["benchmark"]
    params = {k: v for k, v in spec.get("params", {}).items() if not isinstance(v, list)}
    params.update(c["overrides"])
    params.setdefault("benchmark", bench)
    tickers = resolve_universe_tickers(spec)
    if bench not in tickers:
        tickers.append(bench)
    start = date.fromisoformat(str(spec["period"]["start"]))
    end = date.fromisoformat(str(spec["period"]["end"]))
    print(f"[{c['label']}] loading {len(tickers)} tickers ...", flush=True)
    dfs_raw = _load_universe(tickers, start, end, force_refetch=False)
    dfs = {t: _to_date_index(df) for t, df in dfs_raw.items()}
    km = KIND_REGISTRY[spec["kind"]]
    state = km.precompute(dfs_raw, params)
    signals = []
    for t, df in dfs_raw.items():
        if t != bench:
            signals.extend(km.replay(df, t, params, state))
    H = int(params.get("max_hold_days", 21))
    kind = entry_pricing.resolve_kind(spec["kind"])

    refs = {"pivot": [], "open": []}
    slips = {s: [] for s in SLIPS if s > 0}
    filled = []
    gaps = []
    n_missed = n_filled = 0
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
        fts, _row = hit
        fill_pos = int(df.index.get_loc(fts))
        bar = df.loc[fts]
        opn = float(bar["Open"])
        if opn <= 0 or not np.isfinite(opn):
            continue
        fp = _compute_fill(kind, pivot, bar, fill_model=FILL_MARKETABLE_LIMIT,
                           momentum_buffer=MOMENTUM_BUFFER)
        if fp is None:
            n_missed += 1
            refs["pivot"].append(fwd_from(df, fill_pos, H, pivot))
            refs["open"].append(fwd_from(df, fill_pos, H, opn))
            for s in slips:
                slips[s].append(fwd_from(df, fill_pos, H, opn * (1.0 + s)))
            gaps.append(opn / pivot - 1.0)
        else:
            n_filled += 1
            filled.append(fwd_from(df, fill_pos, H, fp))

    f = stats(filled)
    p = stats(refs["pivot"]); o = stats(refs["open"])
    s20 = stats(slips[0.0020]); s50 = stats(slips[0.0050])
    g = stats(gaps)
    ratio = (s20["mean"] / f["mean"]) if f["mean"] > 0 else float("inf")
    out_lines += [f"### {c['label']}", "",
                  f"- signals {len(signals)} -> filled {n_filled} / missed {n_missed} "
                  f"(fill rate {n_filled/max(n_filled+n_missed,1)*100:.0f}%) · H={H} bars, unconditional, gross",
                  f"- missed-cohort overnight gap paid at open: mean **{g['mean']:+.2f}%** (median {g['med']:+.2f}%)", "",
                  "| cohort / reference | n | mean fwd % | median fwd % |", "|---|---|---|---|",
                  f"| FILLED (from modeled fill) | {f['n']} | {f['mean']:+.2f} | {f['med']:+.2f} |",
                  f"| missed, from **pivot** (pre-gap — the verdicts' number) | {p['n']} | {p['mean']:+.2f} | {p['med']:+.2f} |",
                  f"| missed, from **open** (achievable, MOO) | {o['n']} | {o['mean']:+.2f} | {o['med']:+.2f} |",
                  f"| missed, from **open + 20 bps** (pre-registered comparator) | {s20['n']} | {s20['mean']:+.2f} | {s20['med']:+.2f} |",
                  f"| missed, from open + 50 bps (stress) | {s50['n']} | {s50['mean']:+.2f} | {s50['med']:+.2f} |", "",
                  f"- **Stopping-rule ratio: gap-adjusted missed (open+20bps) / filled = "
                  f"{s20['mean']:+.2f}% / {f['mean']:+.2f}% = {ratio:.2f}x** (rule: <~2x -> collapse)", ""]
    print(f"[{c['label']}] filled {f['mean']:+.2f}% | missed pivot {p['mean']:+.2f}% -> open+20 {s20['mean']:+.2f}% | ratio {ratio:.2f}x")
    return ratio, f, s20


def main():
    out = []
    ratios = {}
    for c in COHORTS:
        try:
            ratios[c["label"]] = run_cohort(c, out)
        except Exception as e:  # noqa: BLE001 — insider cohort is optional per handoff
            out += [f"### {c['label']}", "", f"- NOT MEASURABLE in this session: {e!r}", ""]
            print(f"[{c['label']}] failed: {e!r}")
    path = os.path.join(_ROOT, "ledgers", "improvements", "2026-08-06-same-session-entry-doctrine-review.md")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n" + "\n".join(out) + "\n")
    print(f"appended results to {path}")


if __name__ == "__main__":
    main()
