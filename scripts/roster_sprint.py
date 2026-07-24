"""Phase-1b roster-replenishment sprint (2026-07-24): widen the funnel, keep the bar.

Judges three candidates through the FULL current stack, grids exactly as the
spec files declare (every combo is a priced DSR trial):

  1. connors_rsi2        - parked on a STALE pre-concurrent-cap verdict
  2. xs_low_volatility   - spec exists, never gated
  3. event_insider_buying - spec exists, never gated

Per candidate: (a) quant_strategies.runner.run_spec = zero-cost grid + the
two-clause gate + the C1 DSR report; (b) net-of-cost 6-window walk-forward at
deployed/default params; (c) measured OOS daily-return correlation of the
net-of-cost aggregate equity curve vs ts_momentum_liquid_us's. Verdicts are
SURFACED - promotion is the operator's call. Pre-registered expectation
(plans/2026-07-24-second-stream-diversification-plan.md): 0-1 of 3 pass;
0 passes confirms the family-mined-out thesis and is NOT a loosening trigger.

Run: uv run python scripts/roster_sprint.py
"""
from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.auto_paper import config as ap_config
from tools.quant_strategies.runner import run_spec
import scripts.volume_share_slippage_rerun as rerun

CANDIDATES = ["connors_rsi2", "xs_low_volatility", "event_insider_buying"]
BENCH_SETUP = "ts_momentum_liquid_us"
OUT = "ledgers/improvements/2026-07-24-roster-sprint-verdicts.md"


def _rows():
    data = ap_config.load()
    rows = {}
    for section in ("deployable", "parked_by_concurrent_cap_reveals_weak_edge",
                    "parked_by_tightened_gate"):
        for r in data.get(section, []) or []:
            if isinstance(r, dict) and "setup" in r:
                rows.setdefault(r["setup"], r)
    return rows


def _net_wf(setup, rows):
    signals, dfs, spec, start, end = rerun._collect(setup, rows.get(setup, {}))
    wf = rerun._wf(signals, dfs, spec, start, end)
    daily = wf.aggregate.equity_curve.pct_change().dropna()
    return wf, daily, len(signals)


def main() -> int:
    rows = _rows()
    print(f"=== benchmark: {BENCH_SETUP} net walk-forward for correlation", flush=True)
    _, bench_daily, _ = _net_wf(BENCH_SETUP, rows)

    lines = [
        "# Roster-replenishment sprint verdicts (Phase 1b, 2026-07-24)",
        "",
        "Full stack: zero-cost grid gate (runner) + C1 DSR (report-only) +",
        "net-of-cost 6-window walk-forward + measured correlation vs",
        f"{BENCH_SETUP}. Grids exactly as spec files declare. Promotion =",
        "operator's call; pre-registered expectation 0-1 of 3 pass.",
        "",
        "| candidate | grid gate (best combo) | DSR | net agg Sharpe | net windows | net gate | corr vs ts_mom | verdict |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for setup in CANDIDATES:
        print(f"=== {setup}", flush=True)
        try:
            rr = run_spec(f"tools/quant_strategies/{setup}.yml")
            top = rr["combos"][0] if rr["combos"] else None
            grid_pass = bool(top and top.get("gate_passed_under_spec"))
            dsr = rr.get("dsr") or {}
            dsr_s = f"{dsr['dsr']:.3f}" if dsr.get("dsr") is not None else "n/a"
            with open(f"journal/backtest/2026-07-24-sprint-{setup}.md", "w",
                      encoding="utf-8") as fh:
                fh.write(rr["markdown"])
            wf, daily, n_sig = _net_wf(setup, rows)
            joined = daily.align(bench_daily, join="inner")
            corr = (float(joined[0].corr(joined[1]))
                    if len(joined[0]) >= 60 else None)
            corr_s = f"{corr:.2f}" if corr is not None else "n/a"
            net_pass = wf.overall_passed
            verdict = "PASS - operator review" if (grid_pass and net_pass) else "FAIL"
            lines.append(
                f"| {setup} | {'PASS' if grid_pass else 'FAIL'} | {dsr_s} "
                f"| {wf.aggregate.sharpe_annualised:.2f} "
                f"| {wf.n_windows_above_floor}/{wf.n_windows} "
                f"| {'PASS' if net_pass else 'FAIL'} | {corr_s} | **{verdict}** |"
            )
            print(f"{setup}: grid={grid_pass} dsr={dsr_s} "
                  f"netS={wf.aggregate.sharpe_annualised:.2f} net={net_pass} "
                  f"corr={corr_s}", flush=True)
        except Exception as exc:
            traceback.print_exc()
            lines.append(f"| {setup} | ERROR | | | | | | {exc!r} — not gateable as-is |")
            print(f"{setup}: ERROR {exc!r}", flush=True)

    lines += [
        "",
        "- Full grid reports: `journal/backtest/2026-07-24-sprint-<setup>.md`",
        "- Next: re-derive the trial registry "
        "(`uv run python -m tools.backtest.dsr_gate derive --write`).",
    ]
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
