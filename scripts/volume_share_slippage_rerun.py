"""B4 volume-share slippage re-run: roster x 6-window walk-forward, OFF vs ON.

For each setup (the live survivor + the recently-retired generics as cheap
corroboration, per the 2026-07-24 cherry-pick spec) this collects signals
with DEPLOYED params exactly as scripts/net_gate_rerun.py does, then runs
``portfolio_simulator.simulate_walk_forward`` twice — baseline vs
``volume_share_slippage=True`` (volume_limit 10%, price_impact 0.1) — and
emits a per-window diff table. A setup whose edge collapses under realistic
volume-capped fills was a liquidity mirage.

Read-only vs the roster: failures are SURFACED for the operator, never
auto-retired. Output: journal/backtest/2026-07-24-volume-share-slippage-rerun.md

Usage: uv run python scripts/volume_share_slippage_rerun.py [--setup NAME] [--out PATH]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from tools.auto_paper import config, quant_scanner
from tools.backtest import portfolio_simulator as psim
from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._universe import resolve_universe_tickers
from tools.quant_strategies.runner import _load_universe

SETUPS = [
    "ts_momentum_liquid_us",          # the live survivor — the one that matters
    "residual_momentum_liquid_us",    # retired 2026-06-17 — corroboration only
    "clenow_momentum_liquid_us",      # retired 2026-06-17 — corroboration only
    "xs_short_term_reversal",         # retired 2026-06-17 — corroboration only
    "xs_short_term_reversal_liquid_us",  # retired 2026-06-17 — corroboration only
]

SLIPPAGE_KW = dict(volume_share_slippage=True, volume_limit=0.10, price_impact=0.1)


def _collect(setup: str, row: dict):
    spec = yaml.safe_load(open(f"tools/quant_strategies/{setup}.yml", encoding="utf-8"))
    kind_mod = KIND_REGISTRY[spec["kind"]]
    params = quant_scanner._live_params_for(row or {}, spec)
    benchmark = params.get("benchmark") or spec["universe"]["benchmark"]
    tickers = resolve_universe_tickers(spec)
    if benchmark not in tickers:
        tickers.append(benchmark)
    start = date.fromisoformat(str(spec["period"]["start"]))
    end = date.fromisoformat(str(spec["period"]["end"]))
    dfs = _load_universe(tickers, start, end, force_refetch=False)
    state = kind_mod.precompute(dfs, params) if hasattr(kind_mod, "precompute") else None
    signals = []
    for t, df in dfs.items():
        if t == benchmark:
            continue
        signals.extend(kind_mod.replay(df, t, params, state))
    return signals, dfs, spec, start, end


def _wf(signals, dfs, spec, start, end, **extra):
    gate = spec.get("gate", {})
    return psim.simulate_walk_forward(
        signals, dfs, start=start, end=end,
        sharpe_min=float(gate.get("sharpe_min", 1.0)),
        max_dd_pct=float(gate.get("max_dd_pct", 25.0)),
        n_min=int(gate.get("n_min", 30)),
        min_window_sharpe=float(gate.get("min_window_sharpe", 0.5)),
        min_window_pass_rate=float(gate.get("min_window_pass_rate", 0.5)),
        **extra,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="journal/backtest/2026-07-24-volume-share-slippage-rerun.md")
    ap.add_argument("--setup", default=None)
    args = ap.parse_args()

    data = config.load()
    rows: dict[str, dict] = {}
    for section in ("deployable", "parked_by_concurrent_cap_reveals_weak_edge",
                    "parked_by_tightened_gate"):
        for r in data.get(section, []) or []:
            if isinstance(r, dict) and "setup" in r:
                rows.setdefault(r["setup"], r)
    setups = [args.setup] if args.setup else SETUPS

    lines = [
        "# B4 volume-share slippage re-run (2026-07-24)",
        "",
        "Baseline = the 2026-06-20 corrected cost/fill model (full spread on",
        "marketable crossings). Slippage = baseline + zipline-ported",
        "volume-share mode: entry fills capped at 10% of bar volume (remainder",
        "cancels — DAY orders), quadratic price impact 0.1x(shares/bar_vol)^2 on",
        "liquidity-demanding transactions. Impact is DOUBLE-counted vs the",
        "sqrt-law cost model by design — this is a stress gate for liquidity",
        "mirages, not a best-estimate. A setup that only passes without the",
        "volume cap was never executable at size.",
        "",
        "Setups beyond ts_momentum_liquid_us are RETIRED — included as",
        "corroboration only. Nothing here auto-retires; operator reviews.",
        "",
    ]
    for setup in setups:
        print(f"=== {setup}", flush=True)
        try:
            signals, dfs, spec, start, end = _collect(setup, rows.get(setup, {}))
            base = _wf(signals, dfs, spec, start, end)
            slip = _wf(signals, dfs, spec, start, end, **SLIPPAGE_KW)
        except Exception as exc:
            lines += [f"## {setup}", "", f"ERROR: {exc!r}", ""]
            print(f"{setup}: ERROR {exc!r}", flush=True)
            continue
        lines += [
            f"## {setup}",
            "",
            f"- signals: {len(signals)} · windows: {base.n_windows}",
            f"- aggregate gate: baseline **{'PASS' if base.overall_passed else 'FAIL'}** "
            f"-> slippage **{'PASS' if slip.overall_passed else 'FAIL'}**"
            + ("  ⚠ **VERDICT FLIPS UNDER REALISTIC FILLS**"
               if base.overall_passed != slip.overall_passed else ""),
            f"- slippage-mode fills: {slip.aggregate.n_filled} filled, "
            f"{slip.aggregate.n_volume_capped} volume-capped, "
            f"{slip.aggregate.n_volume_blocked} volume-blocked",
            "",
            "| window (OOS yr) | Sharpe off | Sharpe ON | Δ | MDD% off | MDD% ON | n off | n ON |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for (spec_w, r_off), (_, r_on) in zip(base.window_results, slip.window_results):
            yr = getattr(spec_w, "in_sample_end", None)
            label = yr.year if yr else "?"
            lines.append(
                f"| {label} | {r_off.sharpe_annualised:.2f} | {r_on.sharpe_annualised:.2f} "
                f"| {r_on.sharpe_annualised - r_off.sharpe_annualised:+.2f} "
                f"| {r_off.max_drawdown_pct:.1f} | {r_on.max_drawdown_pct:.1f} "
                f"| {r_off.n_trades} | {r_on.n_trades} |"
            )
        a_off, a_on = base.aggregate, slip.aggregate
        lines += [
            f"| **aggregate** | {a_off.sharpe_annualised:.2f} | {a_on.sharpe_annualised:.2f} "
            f"| {a_on.sharpe_annualised - a_off.sharpe_annualised:+.2f} "
            f"| {a_off.max_drawdown_pct:.1f} | {a_on.max_drawdown_pct:.1f} "
            f"| {a_off.n_trades} | {a_on.n_trades} |",
            "",
            f"- per-window clause: baseline {base.n_windows_above_floor}/{base.n_windows} "
            f"-> slippage {slip.n_windows_above_floor}/{slip.n_windows}",
            "",
        ]
        print(
            f"{setup}: agg S {a_off.sharpe_annualised:.2f}->{a_on.sharpe_annualised:.2f} "
            f"gate {base.overall_passed}->{slip.overall_passed} "
            f"capped={a_on.n_volume_capped} blocked={a_on.n_volume_blocked}",
            flush=True,
        )

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nwrote {args.out}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
