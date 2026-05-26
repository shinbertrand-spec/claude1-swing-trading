"""Signal-correlation pre-screen: residual_momentum vs clenow_momentum.

Closes the marginal-passer caveat on residual_momentum_liquid_us (Sharpe 1.09,
1/6 combos passing — barely deployable). The hypothesis stated in the
strategy spec was that residualisation strips beta-amplified market drift,
so the top-K should be DIFFERENT names than raw Clenow ranks. If the two
strategies end up picking mostly the same names, residual is essentially a
tilted Clenow with no diversification benefit, and the live edge is dominated
by what Clenow already captures.

Pre-screen verdict thresholds (from session memo):
- mean selection overlap > 0.5 → downgrade residual to research-grade
- 0.3 - 0.5 → keep deployable WITH caveat (signal correlation note)
- < 0.3 → keep deployable (genuine alpha source)

Run::

    uv run python scripts/signal_correlation_residual_vs_clenow.py

Writes: journal/backtest/signal_correlation_residual_vs_clenow.md
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.backtest import metrics, simulator, walk_forward  # noqa: E402
from tools.backtest.data_cache import fetch, load  # noqa: E402
from tools.backtest.trailing_stop import TrailConfig  # noqa: E402
from tools.quant_strategies._kinds import clenow_momentum, residual_momentum  # noqa: E402
from tools.quant_strategies._universe import get_universe  # noqa: E402


UNIVERSE = "liquid_us_2026q2"
BENCHMARK = "SPY"
START = date(2017, 1, 1)
END = date(2026, 5, 25)

# Matched top-passing combo: lookback=90, top_k=10
# (Clenow rank 2 by Sharpe 1.47; residual rank 1 by Sharpe 1.09 — only
#  combo that clears the gate.)
PARAMS = {
    "regime_filter_period": 200,
    "rebalance_period_days": 5,
    "atr_period": 20,
    "atr_stop_multiple": 3.0,
    "max_hold_days": 5,
    "target_r_multiple": None,
    "risk_per_trade": 0.01,
    "benchmark": BENCHMARK,
    "momentum_lookback_days": 90,
    "top_k": 10,
}


def load_universe_dfs() -> dict[str, pd.DataFrame]:
    """Fetch + load every ticker. Mirrors runner._load_universe."""
    tickers = get_universe(UNIVERSE)
    if BENCHMARK not in tickers:
        tickers = tickers + [BENCHMARK]
    out: dict[str, pd.DataFrame] = {}
    n_fail = 0
    for i, t in enumerate(tickers, 1):
        if i % 100 == 0:
            print(f"  loaded {i}/{len(tickers)}...", flush=True)
        try:
            fetch(t, start=START, end=END, force_refetch=False)
        except Exception as exc:
            n_fail += 1
            continue
        df = load(t)
        df = walk_forward.trim_ohlcv(df, START, END)
        if len(df) > 0:
            out[t] = df
    print(f"  loaded {len(out)} tickers ({n_fail} fetch failures)", flush=True)
    return out


def compute_selection_overlap(
    ranks_a: dict, ranks_b: dict,
) -> dict:
    """Per-rebalance overlap stats between two ranks_by_date dicts."""
    common_dates = sorted(set(ranks_a.keys()) & set(ranks_b.keys()))
    overlaps_jac = []  # jaccard |A∩B| / |A∪B|
    overlaps_share = []  # |A∩B| / |A|  (= jaccard when |A|=|B|=K)
    per_date = []
    for d in common_dates:
        a, b = ranks_a[d], ranks_b[d]
        inter = a & b
        union = a | b
        if not union:
            continue
        jac = len(inter) / len(union)
        share = len(inter) / len(a) if a else 0.0
        overlaps_jac.append(jac)
        overlaps_share.append(share)
        per_date.append({
            "date": d,
            "n_a": len(a),
            "n_b": len(b),
            "n_inter": len(inter),
            "jaccard": jac,
            "share_of_a": share,
        })

    if not overlaps_jac:
        return {"n_dates": 0, "per_date": []}

    return {
        "n_dates": len(overlaps_jac),
        "mean_jaccard": float(np.mean(overlaps_jac)),
        "median_jaccard": float(np.median(overlaps_jac)),
        "mean_share": float(np.mean(overlaps_share)),
        "median_share": float(np.median(overlaps_share)),
        "min_jaccard": float(np.min(overlaps_jac)),
        "max_jaccard": float(np.max(overlaps_jac)),
        "per_date": per_date,
    }


def run_full_pipeline(kind_mod, universe_dfs: dict, params: dict) -> tuple:
    """Run precompute → replay → simulate → cap. Returns (state, capped_outcomes)."""
    state = kind_mod.precompute(universe_dfs, params)
    trail_cfg = TrailConfig(mode="fixed")
    all_outcomes = []
    benchmark = params["benchmark"]
    for t, df in universe_dfs.items():
        if t == benchmark:
            continue
        signals = kind_mod.replay(df, t, params, state)
        if not signals:
            continue
        outs = simulator.simulate_signals(signals, df, trail_config=trail_cfg)
        all_outcomes.extend(outs)
    capped = metrics._apply_concurrent_cap(
        all_outcomes, metrics.DEFAULT_MAX_CONCURRENT
    )
    return state, capped


def weekly_pnl_series(outcomes: list, risk_per_trade: float = 0.01) -> pd.Series:
    """Map outcomes to a weekly portfolio-return series indexed by exit-week.

    For each trade, contribute risk_per_trade × r_multiple to the week its
    exit_date falls into. Stacking by exit_date matches the equity-curve
    convention in metrics._equity_curve.
    """
    if not outcomes:
        return pd.Series(dtype=float)
    rows = [
        (pd.Timestamp(o.exit_date), risk_per_trade * o.r_multiple)
        for o in outcomes
    ]
    df = pd.DataFrame(rows, columns=["exit_date", "ret"])
    df = df.set_index("exit_date")
    # Sum returns per ISO week (W-FRI ends on Friday — typical equity-week close).
    weekly = df["ret"].resample("W-FRI").sum().fillna(0.0)
    return weekly


def yearly_overlap_breakdown(per_date: list[dict]) -> list[dict]:
    """Group per-date overlap stats by calendar year."""
    by_year: dict[int, list[dict]] = {}
    for entry in per_date:
        y = pd.Timestamp(entry["date"]).year
        by_year.setdefault(y, []).append(entry)
    out = []
    for y, entries in sorted(by_year.items()):
        jacs = [e["jaccard"] for e in entries]
        out.append({
            "year": y,
            "n": len(entries),
            "mean_jaccard": float(np.mean(jacs)),
            "min_jaccard": float(np.min(jacs)),
            "max_jaccard": float(np.max(jacs)),
        })
    return out


def main() -> None:
    print("# Signal-correlation pre-screen: residual_momentum vs clenow_momentum")
    print(f"# Universe: {UNIVERSE} · Period: {START} → {END}")
    print(f"# Matched params: lookback={PARAMS['momentum_lookback_days']}, "
          f"top_k={PARAMS['top_k']}, weekly rebalance")
    print()

    print("[1/4] Loading universe...", flush=True)
    universe_dfs = load_universe_dfs()

    print("[2/4] Running Clenow precompute + replay...", flush=True)
    clenow_state, clenow_outs = run_full_pipeline(
        clenow_momentum, universe_dfs, PARAMS,
    )
    print(f"  Clenow: {len(clenow_state.ranks_by_date)} rebalance dates, "
          f"{len(clenow_outs)} capped trades", flush=True)

    print("[3/4] Running Residual precompute + replay...", flush=True)
    resid_state, resid_outs = run_full_pipeline(
        residual_momentum, universe_dfs, PARAMS,
    )
    print(f"  Residual: {len(resid_state.ranks_by_date)} rebalance dates, "
          f"{len(resid_outs)} capped trades", flush=True)

    print("[4/4] Computing overlap + return correlation...", flush=True)

    # Selection overlap
    sel = compute_selection_overlap(
        clenow_state.ranks_by_date, resid_state.ranks_by_date,
    )
    yearly = yearly_overlap_breakdown(sel.get("per_date", []))

    # Return correlation — weekly P&L correlation between the two strategies
    clenow_weekly = weekly_pnl_series(clenow_outs)
    resid_weekly = weekly_pnl_series(resid_outs)
    aligned = pd.concat(
        [clenow_weekly.rename("clenow"), resid_weekly.rename("resid")],
        axis=1,
    ).fillna(0.0)
    # Drop weeks where BOTH are zero (no trades either side)
    nonzero = aligned[(aligned["clenow"] != 0) | (aligned["resid"] != 0)]
    pearson = float(nonzero["clenow"].corr(nonzero["resid"])) if len(nonzero) > 5 else float("nan")

    # Trade-ticker overlap (union of all ticker-week pairs actually traded)
    clenow_trades = {(o.signal.ticker, o.signal.fill_date) for o in clenow_outs}
    resid_trades = {(o.signal.ticker, o.signal.fill_date) for o in resid_outs}
    trade_overlap_jac = (
        len(clenow_trades & resid_trades) / len(clenow_trades | resid_trades)
        if (clenow_trades | resid_trades) else 0.0
    )

    # ---- Decide verdict ----
    mean_jac = sel.get("mean_jaccard", 0.0)
    if mean_jac > 0.5 or pearson > 0.5:
        verdict = "DOWNGRADE — high overlap or return correlation"
        verdict_emoji = "❌"
    elif mean_jac > 0.3 or pearson > 0.3:
        verdict = "KEEP WITH CAVEAT — moderate overlap"
        verdict_emoji = "⚠️"
    else:
        verdict = "KEEP DEPLOYABLE — low overlap, genuine alpha source"
        verdict_emoji = "✅"

    # ---- Write Markdown report ----
    out_path = ROOT / "journal" / "backtest" / "signal_correlation_residual_vs_clenow.md"
    lines = [
        "# Signal-correlation pre-screen — residual_momentum vs clenow_momentum",
        "",
        f"- Universe: **{UNIVERSE}** ({len(universe_dfs)} tickers + {BENCHMARK} benchmark)",
        f"- Period: {START} → {END}",
        f"- Matched params: `momentum_lookback_days={PARAMS['momentum_lookback_days']}` · "
        f"`top_k={PARAMS['top_k']}` · `rebalance_period_days={PARAMS['rebalance_period_days']}`",
        f"- Max concurrent cap: {metrics.DEFAULT_MAX_CONCURRENT} (per CLAUDE.md hard rule)",
        "",
        "## Hypothesis under test",
        "",
        "Residual momentum strips market-beta exposure before ranking, so its",
        "top-K should pick *idiosyncratically* strong names rather than",
        "beta-amplified market drift. If the two strategies pick mostly the",
        "same names at the same time, residualisation provides no",
        "diversification value and residual_momentum_liquid_us is essentially",
        "a tilted clenow_momentum_liquid_us with weaker realised Sharpe",
        "(1.09 vs 1.47).",
        "",
        "**Decision thresholds:**",
        "- Mean Jaccard > 0.5 OR weekly return correlation > 0.5 → downgrade to research",
        "- 0.3 - 0.5 → keep deployable with caveat",
        "- < 0.3 → genuinely independent signal, keep deployable",
        "",
        f"## Verdict: {verdict_emoji} {verdict}",
        "",
        "## Selection overlap (top-K Jaccard, per-rebalance, full period)",
        "",
        f"- Rebalance dates compared: **{sel['n_dates']}**",
        f"- Mean Jaccard `|A∩B| / |A∪B|`: **{mean_jac:.3f}**",
        f"- Median Jaccard: {sel.get('median_jaccard', 0.0):.3f}",
        f"- Mean share `|A∩B| / |A|`: {sel.get('mean_share', 0.0):.3f}",
        f"- Min / max Jaccard: {sel.get('min_jaccard', 0.0):.3f} / {sel.get('max_jaccard', 0.0):.3f}",
        "",
        "### Per-year breakdown",
        "",
        "| Year | n rebals | mean Jaccard | min | max |",
        "|---|---|---|---|---|",
    ]
    for y in yearly:
        lines.append(
            f"| {y['year']} | {y['n']} | {y['mean_jaccard']:.3f} | "
            f"{y['min_jaccard']:.3f} | {y['max_jaccard']:.3f} |"
        )
    lines.extend([
        "",
        "## Realised-trade correlation (post-concurrent-cap)",
        "",
        f"- Clenow capped trades: {len(clenow_outs)}",
        f"- Residual capped trades: {len(resid_outs)}",
        f"- Trade (ticker, fill_date) pair overlap (Jaccard): **{trade_overlap_jac:.3f}**",
        f"  - _Lower than selection overlap because the 8-cap rejects different trades_",
        f"  - _on each side — but only the surviving trades drive realised P&L._",
        f"- Weekly P&L correlation (Pearson, exit-date-weekly buckets): **{pearson:.3f}**",
        f"  - Non-zero weeks: {len(nonzero)}",
        "",
        "## Interpretation",
        "",
    ])

    if mean_jac > 0.5:
        lines.extend([
            "Mean Jaccard > 0.5 means residual's top-K and Clenow's top-K agree on",
            "more than half their picks every week. Residualisation IS doing",
            "something (the picks aren't identical), but not enough to justify",
            "carrying a second strategy with weaker realised Sharpe (1.09 vs 1.47).",
            "The Blitz-Huij-Martens 2011 result (residual long-short IR ~0.8 vs raw",
            "IR ~0.5) does not replicate in our top-K-long implementation on this",
            "universe and period.",
            "",
            "**Recommended action**: remove `residual_momentum_liquid_us` from",
            "`tools/deployable_setups.yml`; keep the strategy YAML + tests as",
            "research-grade reference for the multi-factor extension (market +",
            "SMB + HML residualisation) that the v1 caveat notes.",
        ])
    elif mean_jac > 0.3:
        lines.extend([
            "Mean Jaccard 0.3 - 0.5 means residual picks meaningfully different",
            "names from Clenow in roughly half the rebalances, but the overlap",
            "is high enough that the two strategies will be correlated on",
            "drawdowns. The two strategies together do NOT give the diversification",
            "the spec hypothesised (Blitz-Huij-Martens 2011 implied near-independence",
            "between residual and raw signals); they give a tilted-Clenow effect.",
            "",
            "**Recommended action**: keep deployable BUT add a caveat note:",
            "`residual_momentum_liquid_us` should not be sized on top of",
            "`clenow_momentum_liquid_us` for portfolio purposes — treat them as",
            "ONE bucket of momentum exposure for sector/style risk budgeting.",
        ])
    else:
        lines.extend([
            "Mean Jaccard < 0.3 means residual genuinely picks different names",
            "from Clenow — the residualisation step IS doing the work the spec",
            "claimed. Despite weaker single-strategy Sharpe (1.09 vs 1.47), the",
            "two could be sized together for portfolio diversification.",
            "",
            "**Recommended action**: keep deployable; document the low",
            "selection-overlap finding as supporting evidence for the spec's",
            "hypothesis. Consider running both as parallel sleeves with separate",
            "risk budgets.",
        ])

    lines.extend([
        "",
        "## Provenance",
        "",
        f"- Script: `scripts/signal_correlation_residual_vs_clenow.py`",
        f"- Generated: {date.today().isoformat()}",
    ])

    text = "\n".join(lines)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print()
    print(text)
    print()
    print(f"# Report written to {out_path}")


if __name__ == "__main__":
    main()
