"""Combinatorial Purged Cross-Validation (cherry-pick Batch C, C2).

Upgrades the 6 point-estimate walk-forward windows to a DISTRIBUTION of
full backtest paths per setup. Splitter semantics ported (code re-written,
MIT reference verified 2026-07-24) from sam31415/timeseriescv
``CombPurgedKFoldCV``: the sample span is divided into ``n_groups``
contiguous groups; every combination of ``n_test_groups`` groups becomes a
test set (C(N,k) combinations); train samples whose label/evaluation window
overlaps a test span are PURGED and an optional post-test EMBARGO drops
trailing train samples. Each group appears in exactly C(N-1, k-1)
combinations, so the per-(combination, group) OOS segments recombine into
C(N-1, k-1) complete out-of-sample paths — the path distribution the
point-estimate gate cannot see.

Honest note carried from the research (and kept true here): with
non-overlapping daily-bar labels, purging/embargo add little — the value is
the path distribution. The roster evaluator therefore keeps embargo small
and relies on SEGMENT TRUNCATION for leakage control: each group is
simulated standalone (positions force-close at the group boundary), so no
holding period ever crosses a train/test boundary. The cost of that choice
— boundary trades truncated, fresh capital per segment — is stated in the
report it writes.

Selection procedure per combination (this is what makes paths differ):
every param combo's per-group return series is precomputed ONCE; for each
combination the best param combo on the TRAIN groups (pooled Sharpe) is
selected and its TEST-group segments are recorded. Path gating proposal per
the C2 spec: 5th-percentile path performance — proposed to the operator
with the evidence table, never auto-applied.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]

TRADING_DAYS = 252


def n_paths(n_groups: int, n_test_groups: int) -> int:
    """Each group appears in C(N-1, k-1) combinations -> that many paths."""
    return math.comb(n_groups - 1, n_test_groups - 1)


def path_assignment(n_groups: int, n_test_groups: int) -> dict[tuple[int, int], int]:
    """(combo_id, group) -> path_id. The j-th occurrence of each group across
    the combination sequence goes to path j, giving C(N-1,k-1) paths that
    each cover every group exactly once (asserted in tests)."""
    occurrence: dict[int, int] = {}
    mapping: dict[tuple[int, int], int] = {}
    for combo_id, tg in enumerate(combinations(range(n_groups), n_test_groups)):
        for g in tg:
            j = occurrence.get(g, 0)
            mapping[(combo_id, g)] = j
            occurrence[g] = j + 1
    return mapping


@dataclass
class CPCVSplit:
    combo_id: int
    test_groups: tuple[int, ...]
    train_idx: np.ndarray
    test_idx: np.ndarray


class CombinatorialPurgedCV:
    """Index-level splitter over ordered samples with per-sample prediction
    and evaluation times (any comparable scalar — ints, dates)."""

    def __init__(self, n_groups: int = 12, n_test_groups: int = 2,
                 embargo: float = 0.0):
        if n_groups < 2 or not (1 <= n_test_groups < n_groups):
            raise ValueError(
                f"need n_groups >= 2 and 1 <= n_test_groups < n_groups, "
                f"got {n_groups}/{n_test_groups}"
            )
        self.n_groups = n_groups
        self.n_test_groups = n_test_groups
        self.embargo = embargo

    def group_bounds(self, n_samples: int) -> list[tuple[int, int]]:
        """Contiguous [start, end) position bounds via even split."""
        splits = np.array_split(np.arange(n_samples), self.n_groups)
        return [(int(s[0]), int(s[-1]) + 1) for s in splits if len(s)]

    def split(
        self,
        pred_times: Sequence[Any],
        eval_times: Sequence[Any],
    ) -> Iterator[CPCVSplit]:
        """Yield every combination's (train, test) position indices.

        Purge + embargo containment rule (reference semantics): a train
        sample survives a test span iff its evaluation time ends BEFORE the
        test span's first prediction time, OR its prediction time starts
        AFTER the test span's last evaluation time + embargo.
        """
        if len(pred_times) != len(eval_times):
            raise ValueError("pred_times and eval_times must be same length")
        n = len(pred_times)
        bounds = self.group_bounds(n)
        if len(bounds) < self.n_groups:
            raise ValueError(
                f"{n} samples cannot fill {self.n_groups} non-empty groups"
            )
        pred = list(pred_times)
        ev = list(eval_times)
        for combo_id, tg in enumerate(
            combinations(range(self.n_groups), self.n_test_groups)
        ):
            test_idx: list[int] = []
            spans: list[tuple[Any, Any]] = []
            for g in tg:
                s, e = bounds[g]
                test_idx.extend(range(s, e))
                spans.append((pred[s], max(ev[s:e])))
            test_set = set(test_idx)
            train_idx = [
                i for i in range(n)
                if i not in test_set and all(
                    ev[i] < span_start or pred[i] > self._embargoed(span_end)
                    for span_start, span_end in spans
                )
            ]
            yield CPCVSplit(
                combo_id=combo_id, test_groups=tg,
                train_idx=np.asarray(train_idx, dtype=int),
                test_idx=np.asarray(sorted(test_idx), dtype=int),
            )

    def _embargoed(self, span_end: Any):
        if not self.embargo:
            return span_end
        try:
            return span_end + self.embargo
        except TypeError:
            from datetime import timedelta
            return span_end + timedelta(days=self.embargo)


# ---------------------------------------------------------------------------
# Roster evaluation: path distribution for a deployed setup
# ---------------------------------------------------------------------------

@dataclass
class PathStats:
    path_id: int
    sharpe_ann: float
    max_dd_pct: float
    cum_return_pct: float
    n_days: int
    params_by_group: dict[int, str] = field(default_factory=dict)


def _series_stats(returns: np.ndarray) -> tuple[float, float, float]:
    if len(returns) < 3:
        return 0.0, 0.0, 0.0
    mean, std = float(np.mean(returns)), float(np.std(returns, ddof=1))
    sharpe = (mean / std) * math.sqrt(TRADING_DAYS) if std > 0 else 0.0
    curve = np.cumprod(1.0 + returns)
    peak = np.maximum.accumulate(curve)
    mdd = float(np.min((curve - peak) / peak)) * 100
    return sharpe, mdd, float(curve[-1] - 1.0) * 100


def evaluate_setup_cpcv(
    setup: str,
    *,
    n_groups: int = 12,
    n_test_groups: int = 2,
) -> dict[str, Any]:
    """CPCV path distribution for a roster setup over its full spec period.

    Precomputes per (param-combo, group) daily returns (each group simulated
    standalone — fresh capital, positions force-closed at the boundary),
    then per CPCV combination selects the best param combo on the train
    groups' pooled Sharpe and records its test-group segments into paths.
    """
    import pandas as pd
    import yaml as _yaml

    import scripts.volume_share_slippage_rerun as rerun
    from ..auto_paper import config as ap_config, quant_scanner
    from ..quant_strategies._kinds import KIND_REGISTRY
    from ..quant_strategies.runner import _expand_grid, _load_universe
    from ..quant_strategies._universe import resolve_universe_tickers
    from . import portfolio_simulator as psim

    spec = _yaml.safe_load(
        open(_ROOT / "tools" / "quant_strategies" / f"{setup}.yml", encoding="utf-8")
    )
    kind_mod = KIND_REGISTRY[spec["kind"]]
    benchmark = spec["universe"]["benchmark"]
    tickers = resolve_universe_tickers(spec)
    if benchmark not in tickers:
        tickers.append(benchmark)
    start = date.fromisoformat(str(spec["period"]["start"]))
    end = date.fromisoformat(str(spec["period"]["end"]))
    dfs = _load_universe(tickers, start, end, force_refetch=False)

    raw_params = dict(spec.get("params", {}))
    raw_params.setdefault("benchmark", benchmark)
    param_combos = _expand_grid(raw_params)

    # Master calendar + contiguous date groups.
    all_ts = sorted({ts for df in dfs.values() for ts in df.index})
    cv = CombinatorialPurgedCV(n_groups=n_groups, n_test_groups=n_test_groups)
    bounds = cv.group_bounds(len(all_ts))
    group_ranges = [(all_ts[s].date(), all_ts[e - 1].date()) for s, e in bounds]

    def _label(params: dict) -> str:
        return ",".join(f"{k}={v}" for k, v in sorted(params.items())
                        if k != "benchmark")

    # Precompute per (param, group) daily returns.
    seg_returns: dict[tuple[int, int], np.ndarray] = {}
    labels: list[str] = []
    for pi, params in enumerate(param_combos):
        labels.append(_label(params))
        state = (kind_mod.precompute(dfs, params)
                 if hasattr(kind_mod, "precompute") else None)
        signals = []
        for t, df in dfs.items():
            if t == benchmark:
                continue
            signals.extend(kind_mod.replay(df, t, params, state))
        for gi, (g0, g1) in enumerate(group_ranges):
            g_sigs = [s for s in signals if g0 <= s.fill_date <= g1]
            g_dfs = {
                t: df.loc[(df.index.date >= g0) & (df.index.date <= g1)]
                for t, df in dfs.items()
            }
            g_dfs = {t: df for t, df in g_dfs.items() if not df.empty}
            if not g_sigs or not g_dfs:
                seg_returns[(pi, gi)] = np.zeros(0)
                continue
            res = psim.simulate(g_sigs, g_dfs)
            seg_returns[(pi, gi)] = (
                res.equity_curve.pct_change().dropna().to_numpy()
                if len(res.equity_curve) else np.zeros(0)
            )

    # CPCV combinations: select on train, record test segments into paths.
    mapping = path_assignment(n_groups, n_test_groups)
    paths_segments: dict[int, dict[int, tuple[np.ndarray, str]]] = {}
    for combo_id, tg in enumerate(combinations(range(n_groups), n_test_groups)):
        train_groups = [g for g in range(n_groups) if g not in tg]
        best_pi, best_sharpe = 0, -np.inf
        for pi in range(len(param_combos)):
            pooled = np.concatenate(
                [seg_returns[(pi, g)] for g in train_groups]
            ) if train_groups else np.zeros(0)
            s, _, _ = _series_stats(pooled)
            if s > best_sharpe:
                best_pi, best_sharpe = pi, s
        for g in tg:
            pid = mapping[(combo_id, g)]
            paths_segments.setdefault(pid, {})[g] = (
                seg_returns[(best_pi, g)], labels[best_pi],
            )

    paths: list[PathStats] = []
    for pid in sorted(paths_segments):
        segs = paths_segments[pid]
        series = np.concatenate([segs[g][0] for g in sorted(segs)])
        sharpe, mdd, cum = _series_stats(series)
        paths.append(PathStats(
            path_id=pid, sharpe_ann=sharpe, max_dd_pct=mdd,
            cum_return_pct=cum, n_days=len(series),
            params_by_group={g: segs[g][1] for g in sorted(segs)},
        ))

    sharpes = sorted(p.sharpe_ann for p in paths)
    p5 = float(np.percentile(sharpes, 5)) if sharpes else 0.0
    return {
        "setup": setup,
        "n_groups": n_groups, "n_test_groups": n_test_groups,
        "n_combinations": math.comb(n_groups, n_test_groups),
        "n_paths": len(paths),
        "param_combos": labels,
        "group_ranges": [(str(a), str(b)) for a, b in group_ranges],
        "paths": paths,
        "sharpe_min": min(sharpes) if sharpes else None,
        "sharpe_median": float(np.median(sharpes)) if sharpes else None,
        "sharpe_p5": p5,
    }


def render_markdown(result: dict[str, Any]) -> str:
    L = [
        f"# CPCV path distribution — {result['setup']} (C2, 2026-07-24)",
        "",
        f"- groups N={result['n_groups']} x test k={result['n_test_groups']} -> "
        f"{result['n_combinations']} combinations, {result['n_paths']} recombined OOS paths",
        f"- param combos in selection grid: {', '.join(result['param_combos'])}",
        "- each group simulated standalone (fresh capital; positions force-close "
        "at the group boundary) — leakage controlled by truncation, so purging "
        "adds nothing extra with daily non-overlapping labels (kept per spec note)",
        "",
        "| path | Sharpe (ann) | maxDD % | cum % | days | param selection (by group) |",
        "|---|---|---|---|---|---|",
    ]
    for p in result["paths"]:
        sel = "; ".join(sorted({v for v in p.params_by_group.values()}))
        L.append(
            f"| {p.path_id} | {p.sharpe_ann:.2f} | {p.max_dd_pct:.1f} "
            f"| {p.cum_return_pct:.1f} | {p.n_days} | {sel} |"
        )
    L += [
        "",
        f"- **path-Sharpe distribution: min {result['sharpe_min']:.2f} · "
        f"5th pctile {result['sharpe_p5']:.2f} · median {result['sharpe_median']:.2f}**",
        "",
        "## Proposed gate (operator decision — NOT auto-applied)",
        "",
        "- proposal: 5th-percentile path Sharpe > 0.0 (no recombined OOS path "
        "regime in which the setup loses money risk-adjusted), evaluated "
        "alongside the existing two-clause gate + C1 DSR",
        f"- this setup under that proposal: "
        f"**{'PASS' if result['sharpe_p5'] > 0.0 else 'FAIL — surfaced for review'}**",
    ]
    return "\n".join(L) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="CPCV path-distribution evaluation (C2)")
    ap.add_argument("--setup", required=True)
    ap.add_argument("--n-groups", type=int, default=12)
    ap.add_argument("--n-test-groups", type=int, default=2)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    result = evaluate_setup_cpcv(
        args.setup, n_groups=args.n_groups, n_test_groups=args.n_test_groups,
    )
    md = render_markdown(result)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
