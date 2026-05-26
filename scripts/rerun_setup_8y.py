"""One-off: re-run a SETUP_REPLAY_REGISTRY backtest on 2017-2025 history.

Programmatic invocation of :func:`tools.backtest.runner.run_rolling` so a
registered universe + extended period can be passed without CLI-quoting
gymnastics. Writes the markdown report to ``journal/backtest/{setup}-8y.md``.

Usage:
    uv run python scripts/rerun_setup_8y.py SEPA-VCP --sell-aware
    uv run python scripts/rerun_setup_8y.py EP --ma-trail
    uv run python scripts/rerun_setup_8y.py SEPA-VCP --universe sp500_2026q2 --sell-aware
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.backtest import runner
from tools.backtest.sell_aware import SellPolicy
from tools.quant_strategies._universe import get_universe


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("setup", choices=["SEPA-VCP", "EP"],
                   help="Setup name from SETUP_REPLAY_REGISTRY")
    p.add_argument("--start", default="2017-01-01")
    p.add_argument("--end", default="2026-05-25")
    p.add_argument("--trail", default="fixed",
                   choices=["fixed", "ratchet", "ma_trail"])
    p.add_argument("--sell-aware", action="store_true",
                   help="Enable per-bar sell-decision composer exits")
    p.add_argument("--universe", default="sp500_leaning_88",
                   help="Registered universe name (see tools/quant_strategies/_universes/).")
    p.add_argument("--out", default=None,
                   help="Output markdown path (default journal/backtest/<setup>-8y.md)")
    args = p.parse_args()

    tickers = get_universe(args.universe)
    if "SPY" not in tickers:
        tickers.append("SPY")

    sell_policy = SellPolicy(enabled=True) if args.sell_aware else None

    print(f"# Re-running {args.setup} (trail={args.trail}, "
          f"sell_aware={bool(sell_policy)}) on {len(tickers)} tickers "
          f"{args.start}..{args.end}", flush=True)

    result = runner.run_rolling(
        setup=args.setup,
        tickers=tickers,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        is_years=3,
        oos_years=1,
        step_years=1,
        trail=args.trail,
        sell_policy=sell_policy,
        force_refetch=False,  # cache was populated by refetch_universe_2017.py
    )

    md = result["markdown"]
    out_path = Path(
        args.out
        or f"journal/backtest/{args.setup.lower()}-8y.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")

    gate = result["aggregate_gate"]
    print(f"\n## Verdict")
    print(f"- Aggregate: Sharpe {gate.aggregate_sharpe:.2f} · "
          f"|MDD| {abs(gate.aggregate_max_dd_pct):.2f}% · "
          f"n {gate.aggregate_n_trades} → "
          f"{'PASS' if gate.aggregate_gate_passed else 'FAIL'}")
    print(f"- Per-window: {gate.n_windows_above_floor}/{gate.n_windows} "
          f"clear Sharpe > {gate.min_window_sharpe} (rate "
          f"{gate.window_pass_rate:.2f}) → "
          f"{'PASS' if gate.window_clause_passed else 'FAIL'}")
    print(f"- COMPOSITE: {'PASS' if gate.gate_passed else 'FAIL'}")
    if gate.note:
        print(f"- Note: {gate.note}")
    print(f"\nFull report → {out_path}")


if __name__ == "__main__":
    main()
