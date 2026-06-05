"""Sweep the 5 AI-thematic strategy variants through the Phase 5 backtest +
deployment-gate pipeline. Bakes in the two Alfred deltas that govern this run:

Delta 1 — split-gate by universe narrowness:
  ai_thematic_pure   → Sharpe>1.2 ∧ |MDD|<22% ∧ n≥30 ∧ per-window≥60%
  ai_thematic_broad  → Sharpe>1.0 ∧ |MDD|<25% ∧ n≥30 ∧ per-window≥50% (default)

Delta 2 — top-3-contributor concentration diagnostic (ai-pure only):
  For each ai-pure variant, compute what % of OOS PnL comes from the top-3
  contributing tickers. >50% = flag for manual review. Catches single-name
  idiosyncrasy masquerading as edge on narrow universes.

The gate thresholds themselves are baked into each strategy YAML's ``gate:``
block; this sweep just maps each variant to a gate-profile label for
reporting + computes the top-3 diagnostic post-hoc from the runner's
``concat_oos_outcomes``.

Outputs:
- ``journal/backtest-sweep/2026-05-29-ai-thematic.md`` (summary table)
- ``journal/backtest/<variant>-ai-thematic.md`` per variant (detailed)

Usage::

    uv run python scripts/backtest_sweep_ai_thematic.py
    uv run python scripts/backtest_sweep_ai_thematic.py --only xs_short_term_reversal_ai_pure
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.quant_strategies.runner import run_spec


def _safe_print(s: str) -> None:
    """Print to stdout, falling back to UTF-8 bytes when the console codec
    (Windows cp1252) can't encode the string."""
    try:
        print(s, flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(s.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")
        sys.stdout.buffer.flush()


# variant → (gate_profile_label, spec_path). gate_profile is documentary;
# actual thresholds live in the spec's gate: block.
VARIANTS: dict[str, tuple[str, str]] = {
    "xs_short_term_reversal_ai_pure":
        ("ai_thematic_pure", "tools/quant_strategies/xs_short_term_reversal_ai_pure.yml"),
    "xs_short_term_reversal_ai_broad":
        ("ai_thematic_broad", "tools/quant_strategies/xs_short_term_reversal_ai_broad.yml"),
    "connors_rsi2_ai_pure":
        ("ai_thematic_pure", "tools/quant_strategies/connors_rsi2_ai_pure.yml"),
    "connors_rsi2_ai_broad":
        ("ai_thematic_broad", "tools/quant_strategies/connors_rsi2_ai_broad.yml"),
    "clenow_momentum_ai_broad":
        ("ai_thematic_broad", "tools/quant_strategies/clenow_momentum_ai_broad.yml"),
}

TOP_K_CONTRIBUTOR = 3
TOP_K_FLAG_THRESHOLD = 0.50  # > 50% of OOS PnL from top-3 = flag

SWEEP_OUT = Path("journal/backtest-sweep/2026-05-29-ai-thematic.md")
PER_VARIANT_OUT_DIR = Path("journal/backtest")


def compute_top_k_contributor(
    concat_oos_outcomes: list,
    k: int = TOP_K_CONTRIBUTOR,
) -> tuple[float, list[tuple[str, float]]]:
    """Return (top-k contribution fraction of total |PnL|, [(ticker, sum_pnl_pct), ...]).

    Contribution = sum of pnl_pct per ticker. Fraction is sum of top-k
    *absolute* contributions divided by total |PnL| (so a single big winner
    on a narrow universe surfaces clearly; a big loser counts too).

    Returns (0.0, []) if there are no outcomes.
    """
    if not concat_oos_outcomes:
        return 0.0, []
    per_ticker: dict[str, float] = defaultdict(float)
    for o in concat_oos_outcomes:
        per_ticker[o.signal.ticker] += float(o.pnl_pct)
    ranked = sorted(per_ticker.items(), key=lambda kv: abs(kv[1]), reverse=True)
    total_abs = sum(abs(v) for v in per_ticker.values())
    if total_abs == 0.0:
        return 0.0, ranked[:k]
    top_k_abs = sum(abs(v) for _t, v in ranked[:k])
    return top_k_abs / total_abs, ranked[:k]


def _format_top_k(top_k_pairs: list[tuple[str, float]]) -> str:
    return ", ".join(f"{t} ({pnl * 100:+.1f}%)" for t, pnl in top_k_pairs)


def sweep_one(variant: str, gate_profile: str, spec_path: str) -> dict:
    _safe_print(f"\n{'=' * 70}\n# {variant} ({gate_profile})\n{'=' * 70}")
    result = run_spec(spec_path, force_refetch=False)
    combos = result["combos"]
    top_combo = combos[0]  # gate-passers first, then by Sharpe (per runner)

    # Top-3-contributor diagnostic on the top combo's concat OOS outcomes.
    is_ai_pure = gate_profile == "ai_thematic_pure"
    concat_oos = top_combo.get("concat_oos_outcomes", [])
    top_k_frac, top_k_pairs = compute_top_k_contributor(concat_oos, k=TOP_K_CONTRIBUTOR)
    top_k_flag = (top_k_frac > TOP_K_FLAG_THRESHOLD) if is_ai_pure else False

    # Pull headline numbers from the top combo.
    rep = top_combo["oos_report"]
    agg = top_combo.get("aggregate_gate")  # AggregateWithWindows
    sharpe = rep.returns.sharpe_annualised
    mdd = abs(rep.returns.max_drawdown_pct)
    n = rep.trades.n_trades

    if agg is not None:
        clause_1_pass = agg.aggregate_gate_passed
        clause_2_pass = agg.window_clause_passed
        n_windows_above = agg.n_windows_above_floor
        n_windows = agg.n_windows
    else:
        clause_1_pass = top_combo["gate_passed_under_spec"]
        clause_2_pass = True
        n_windows_above = 0
        n_windows = 0

    gate_pass = clause_1_pass and clause_2_pass

    # Verdict: gate must pass AND (for ai-pure) top-3 flag must NOT trip
    if not gate_pass:
        verdict = "REJECT"
    elif is_ai_pure and top_k_flag:
        verdict = "REVIEW"  # gate-pass but concentration-flagged
    else:
        verdict = "DEPLOY"

    # Per-variant detail report.
    PER_VARIANT_OUT_DIR.mkdir(parents=True, exist_ok=True)
    detail_path = PER_VARIANT_OUT_DIR / f"{variant}-ai-thematic.md"
    detail_md = (
        result["markdown"]
        + "\n\n"
        + "## Sweep diagnostics (Alfred deltas)\n\n"
        + f"- Gate profile: **{gate_profile}**\n"
        + f"- Top-{TOP_K_CONTRIBUTOR}-contributor share of |PnL|: "
        + (f"**{top_k_frac:.1%}**" if concat_oos else "_(no OOS outcomes)_")
        + (f" → **FLAGGED** (> {TOP_K_FLAG_THRESHOLD:.0%} threshold)"
           if top_k_flag else "")
        + "\n"
        + (f"- Top-{TOP_K_CONTRIBUTOR} contributors: {_format_top_k(top_k_pairs)}\n"
           if top_k_pairs else "")
        + (f"- _Note: top-{TOP_K_CONTRIBUTOR} flag is INFORMATIONAL on ai-broad "
           "variants (no verdict effect); load-bearing on ai-pure._\n"
           if not is_ai_pure else "")
        + f"\n- **Verdict: {verdict}**\n"
    )
    detail_path.write_text(detail_md, encoding="utf-8")
    _safe_print(f"# detail report -> {detail_path}")

    return {
        "variant": variant,
        "gate_profile": gate_profile,
        "sharpe": sharpe,
        "mdd": mdd,
        "n": n,
        "n_windows_above": n_windows_above,
        "n_windows": n_windows,
        "clause_1_pass": clause_1_pass,
        "clause_2_pass": clause_2_pass,
        "gate_pass": gate_pass,
        "top_k_frac": top_k_frac,
        "top_k_pairs": top_k_pairs,
        "top_k_flag": top_k_flag,
        "verdict": verdict,
        "top_combo_params": top_combo["params"],
        "detail_path": str(detail_path),
    }


def render_sweep_markdown(rows: list[dict]) -> str:
    today = date.today().isoformat()
    lines: list[str] = [
        f"# AI-thematic backtest sweep — {today}",
        "",
        "Sweeps the 5 AI-thematic strategy variants (Alfred-refined plan, "
        "see `plans/polymorphic-tickling-avalanche.md`).",
        "",
        "## Sweep configuration",
        "",
        "- Period: 2017-01-01 → 2026-05-25 (rolling walk-forward, IS=3y / OOS=1y / step=1y, 6 windows)",
        "- Concurrent-position cap: 8 (CLAUDE.md hard rule)",
        "- **Delta 1 — split-gate by universe narrowness:**",
        "  - `ai_thematic_pure` → Sharpe>1.2 ∧ |MDD|<22% ∧ n≥30 ∧ per-window≥60%",
        "  - `ai_thematic_broad` → Sharpe>1.0 ∧ |MDD|<25% ∧ n≥30 ∧ per-window≥50% (default)",
        "- **Delta 2 — top-3-contributor diagnostic** (ai-pure only): "
        f">{TOP_K_FLAG_THRESHOLD:.0%} of OOS |PnL| from top-3 tickers = REVIEW flag",
        "",
        "## Summary",
        "",
        "| Variant | Profile | Sharpe | |MDD| | n | Per-window | Top-3 Contrib | Clause-1 | Clause-2 | Top-3 flag | Verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        per_window = (f"{r['n_windows_above']}/{r['n_windows']}"
                      if r['n_windows'] > 0 else "n/a")
        top_k_cell = (f"{r['top_k_frac']:.1%}" if r['top_k_pairs']
                      else "—")
        top_k_flag_cell = ("**FLAGGED**" if r['top_k_flag']
                           else ("OK" if r['gate_profile'] == 'ai_thematic_pure'
                                 else "n/a"))
        verdict_cell = f"**{r['verdict']}**"
        lines.append(
            f"| {r['variant']} | {r['gate_profile']} | "
            f"{r['sharpe']:.2f} | {r['mdd']:.2f}% | {r['n']} | "
            f"{per_window} | {top_k_cell} | "
            f"{'PASS' if r['clause_1_pass'] else 'FAIL'} | "
            f"{'PASS' if r['clause_2_pass'] else 'FAIL'} | "
            f"{top_k_flag_cell} | {verdict_cell} |"
        )

    lines += [
        "",
        "## Per-variant detail",
        "",
    ]
    for r in rows:
        lines.append(f"- **{r['variant']}** ({r['gate_profile']}) → `{r['detail_path']}`")
        if r['top_k_pairs']:
            lines.append(f"  - Top-3 contributors: {_format_top_k(r['top_k_pairs'])}")
        lines.append(f"  - Top combo params: `{r['top_combo_params']}`")
    lines.append("")

    n_deploy = sum(1 for r in rows if r['verdict'] == 'DEPLOY')
    n_review = sum(1 for r in rows if r['verdict'] == 'REVIEW')
    n_reject = sum(1 for r in rows if r['verdict'] == 'REJECT')
    lines += [
        "## Promotion verdict",
        "",
        f"- DEPLOY: {n_deploy}",
        f"- REVIEW (gate-pass but top-3 concentration flagged): {n_review}",
        f"- REJECT: {n_reject}",
        "",
    ]
    if n_deploy == 0 and n_review == 0:
        lines += [
            "## ALL REJECTED — doctrine path",
            "",
            "Per plan Step 4 failure-mode: if all variants FAIL the gate, two",
            "narrow recovery paths are permitted before archive — add SPY-200d",
            "regime filter on mean-reversion (parameter change, not universe",
            "loosening), or widen `ai_thematic_broad` to ~200 tickers",
            "(documented as hypothesis-fitting). Hard line: **do NOT re-run",
            "more than twice.** Three rounds fail → archive to",
            "`journal/backtest-sweep/_archive/` and write a post-mortem.",
            "",
        ]
    else:
        lines += [
            "## Next step",
            "",
            "Run `scripts/promote_ai_thematic_setups.py --report "
            f"{SWEEP_OUT}` to get proposed `deployable_setups.yml` rows for",
            "DEPLOY-verdict variants. REVIEW-verdict variants require manual",
            "decision on whether the top-3 concentration is acceptable; if",
            "yes, paste the row with a HALF-SIZE annotation + top-3-contrib",
            "disclosure. The same edit must backfill `track: generic` on the",
            "existing 6 deployable rows (Alfred Delta 4).",
            "",
        ]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--only", default=None,
                   help="Sweep just this variant (default: all 5)")
    p.add_argument("--out", default=str(SWEEP_OUT))
    args = p.parse_args()

    variants_to_run = [args.only] if args.only else list(VARIANTS)
    for v in variants_to_run:
        if v not in VARIANTS:
            raise SystemExit(f"unknown variant {v!r}; known: {sorted(VARIANTS)}")

    rows: list[dict] = []
    for variant in variants_to_run:
        gate_profile, spec_path = VARIANTS[variant]
        row = sweep_one(variant, gate_profile, spec_path)
        rows.append(row)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md = render_sweep_markdown(rows)
    out_path.write_text(md, encoding="utf-8")
    _safe_print(f"\n# Sweep report -> {out_path}")
    _safe_print(md)


if __name__ == "__main__":
    main()
