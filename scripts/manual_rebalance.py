"""Manual rebalance — fire signals NOW from each deployable strategy.

Workaround for Bug 2 (live rebalance-schedule misalignment). For each
deployable row in tools/deployable_setups.yml, this script:

  1. Loads the universe via data_cache (same as the scanner).
  2. Computes eligibility AT the last benchmark bar (2026-05-22 today)
     by replicating each kind's scoring function inline — NOT relying on
     state.<eligibility_dict>[last_bench] which is empty for kinds whose
     rebalance schedule doesn't include today.
  3. Composes CandidateInputs via the scanner's existing helper (same
     pivot / ATR-stop / sizing / sector-ETF logic).
  4. Runs each through tools.auto_paper.pipeline.place_candidate(dry_run=True)
     for a preview.
  5. If --place is passed, runs the same loop with dry_run=False (real
     paper-Tiger limit-buy orders + ledger writes + positions.json append).

This is a one-shot operational tool — Bug 2 needs a proper post-market
fix (anchor rebalance schedule to a fixed calendar reference). Until then
this script provides a way to manually drive the strategies on any day.

Safety:
- Tiger paper-only (TigerClient() default, no allow_live).
- Deployable filter via tools.auto_paper.config (same as cron path).
- Track-level hard rules (5% / 20% / 8 / 15%) checked by place_candidate.
- Default is dry-run; `--place` is required to actually fire orders.

Usage::

    uv run python scripts/manual_rebalance.py             # dry-run preview
    uv run python scripts/manual_rebalance.py --place     # real placement
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.auto_paper import config  # noqa: E402
from tools.auto_paper.pipeline import CandidateInput, place_candidate  # noqa: E402
from tools.auto_paper.quant_scanner import (  # noqa: E402
    _compute_atr,
    _live_params_for,
    _find_spec_for,
    _refresh_universe,
    _sector_etf_for,
)
from tools.broker.tiger import TigerClient  # noqa: E402
from tools import position_sizer  # noqa: E402
from tools.quant_strategies._kinds import (  # noqa: E402
    clenow_momentum, connors_rsi2, residual_momentum,
    ts_momentum, xs_short_term_reversal,
)
from tools.quant_strategies._universe import resolve_universe_tickers  # noqa: E402
from tools.regime_check import classify_broad  # noqa: E402
from tools.trend_template import compute_from_ticker as tt_from_ticker  # noqa: E402

import yaml


# ---------------------------------------------------- per-kind force-today eligibility


def _eligible_clenow_at(universe_dfs: dict[str, pd.DataFrame], params: dict,
                         signal_date: pd.Timestamp) -> list[str]:
    """Replicate clenow_momentum.precompute's per-rebalance ranking at signal_date.
    Returns tickers ordered BEST-FIRST (highest slope × R²), capped at top_k."""
    bench = params["benchmark"]
    lookback = int(params["momentum_lookback_days"])
    top_k = int(params["top_k"])

    # Regime check at signal_date (SPY > 200d SMA)
    regime_period = int(params.get("regime_filter_period", 200))
    bench_close = universe_dfs[bench]["Close"]
    bench_sma = bench_close.rolling(window=regime_period, min_periods=regime_period).mean()
    if signal_date not in bench_close.index:
        return []
    sma_at = bench_sma.loc[signal_date]
    if pd.isna(sma_at):
        return []
    if bench_close.loc[signal_date] <= sma_at:
        return []

    candidate_tickers = [t for t in universe_dfs if t != bench]
    scores: list[tuple[float, str]] = []
    for t in candidate_tickers:
        tdf = universe_dfs[t]
        if signal_date not in tdf.index:
            continue
        closes_through = tdf["Close"].loc[:signal_date]
        s = clenow_momentum._annualised_log_slope_r2(closes_through, lookback)
        if np.isfinite(s):
            scores.append((s, t))
    scores.sort(reverse=True)  # best (highest score) first
    return [t for _, t in scores[:top_k]]


def _eligible_residual_at(universe_dfs: dict[str, pd.DataFrame], params: dict,
                           signal_date: pd.Timestamp) -> list[str]:
    """Best-first ordered list of residual-momentum top_k picks at signal_date."""
    bench = params["benchmark"]
    lookback = int(params["momentum_lookback_days"])
    top_k = int(params["top_k"])

    regime_period = int(params.get("regime_filter_period", 200))
    bench_close = universe_dfs[bench]["Close"]
    bench_sma = bench_close.rolling(window=regime_period, min_periods=regime_period).mean()
    if signal_date not in bench_close.index:
        return []
    sma_at = bench_sma.loc[signal_date]
    if pd.isna(sma_at) or bench_close.loc[signal_date] <= sma_at:
        return []

    bench_closes_through = bench_close.loc[:signal_date]
    candidate_tickers = [t for t in universe_dfs if t != bench]
    scores: list[tuple[float, str]] = []
    for t in candidate_tickers:
        tdf = universe_dfs[t]
        if signal_date not in tdf.index:
            continue
        ticker_closes_through = tdf["Close"].loc[:signal_date]
        s = residual_momentum._residual_momentum_score(
            ticker_closes_through, bench_closes_through, lookback,
        )
        if np.isfinite(s):
            scores.append((s, t))
    scores.sort(reverse=True)  # best (highest residual score) first
    return [t for _, t in scores[:top_k]]


def _eligible_xs_reversal_at(universe_dfs: dict[str, pd.DataFrame], params: dict,
                              signal_date: pd.Timestamp) -> list[str]:
    """Best-first (biggest-loser-first) ordered list of xs_reversal picks."""
    bench = params["benchmark"]
    lookback = int(params["lookback_days"])
    candidate_tickers = [t for t in universe_dfs if t != bench]
    if "bottom_pct" in params and params["bottom_pct"] is not None:
        bottom_n = max(1, int(len(candidate_tickers) * float(params["bottom_pct"])))
    else:
        bottom_n = int(params["bottom_n"])
    scores: list[tuple[float, str]] = []
    for t in candidate_tickers:
        tdf = universe_dfs[t]
        if signal_date not in tdf.index:
            continue
        closes_through = tdf["Close"].loc[:signal_date]
        r = xs_short_term_reversal._trailing_return(closes_through, lookback)
        if np.isfinite(r):
            scores.append((r, t))
    scores.sort()  # ascending: biggest losers first (= the strategy's signal)
    return [t for _, t in scores[:bottom_n]]


def _eligible_ts_momentum_at(universe_dfs: dict[str, pd.DataFrame], params: dict,
                              signal_date: pd.Timestamp) -> list[str]:
    """ts_momentum has no precompute / cross-sectional state; replicate the
    per-ticker positive-trailing-return check at signal_date, ranked best-first
    (highest trailing return first) so a top-1 slice gives the strongest signal."""
    bench = params["benchmark"]
    lookback = int(params["lookback_days"])
    candidate_tickers = [t for t in universe_dfs if t != bench]
    scored: list[tuple[float, str]] = []
    for t in candidate_tickers:
        tdf = universe_dfs[t]
        if signal_date not in tdf.index:
            continue
        closes_through = tdf["Close"].loc[:signal_date]
        if len(closes_through) < lookback + 1:
            continue
        c_now = float(closes_through.iloc[-1])
        c_then = float(closes_through.iloc[-(lookback + 1)])
        if c_then <= 0 or pd.isna(c_then) or pd.isna(c_now):
            continue
        r = (c_now / c_then) - 1.0
        if r > 0:
            scored.append((r, t))
    scored.sort(reverse=True)  # strongest momentum first
    return [t for _, t in scored]


def _eligible_connors_rsi2_at(universe_dfs: dict[str, pd.DataFrame], params: dict,
                                signal_date: pd.Timestamp) -> list[str]:
    """Best-first (most-oversold-first) ordered list of connors_rsi2 picks."""
    bench = params["benchmark"]
    rsi_period = int(params.get("rsi_period", 2))
    cum_period = int(params.get("cumulative_period", 2))
    threshold = float(params.get("entry_threshold", 15.0))
    regime_period = int(params.get("regime_sma_period", 200))

    bench_close = universe_dfs[bench]["Close"].astype(float)
    bench_sma = bench_close.rolling(window=regime_period, min_periods=regime_period).mean()
    if signal_date not in bench_close.index:
        return []
    sma_at = bench_sma.loc[signal_date]
    if pd.isna(sma_at) or bench_close.loc[signal_date] <= sma_at:
        return []

    candidate_tickers = [t for t in universe_dfs if t != bench]
    scored: list[tuple[float, str]] = []
    for t in candidate_tickers:
        tdf = universe_dfs[t]
        if signal_date not in tdf.index:
            continue
        closes = tdf["Close"].astype(float).loc[:signal_date]
        crsi = connors_rsi2._cumulative_rsi(closes, rsi_period, cum_period)
        v = crsi.iloc[-1] if len(crsi) > 0 else float("nan")
        if pd.notna(v) and v < threshold:
            scored.append((float(v), t))
    scored.sort()  # most oversold (lowest cumulative RSI) first
    max_conc = params.get("max_concurrent_positions")
    if max_conc is not None:
        scored = scored[:int(max_conc)]
    return [t for _, t in scored]


KIND_TO_FORCE_TODAY = {
    "clenow_momentum": _eligible_clenow_at,
    "residual_momentum": _eligible_residual_at,
    "xs_short_term_reversal": _eligible_xs_reversal_at,
    "ts_momentum": _eligible_ts_momentum_at,
    "connors_rsi2": _eligible_connors_rsi2_at,
}


# ---------------------------------------------------- per-row pipeline


def _last_bench_date(universe_dfs: dict[str, pd.DataFrame], benchmark: str) -> pd.Timestamp:
    if benchmark not in universe_dfs or len(universe_dfs[benchmark]) == 0:
        raise RuntimeError(f"benchmark {benchmark!r} has no bars")
    return universe_dfs[benchmark].index[-1]


def _candidates_for_row(
    row: dict[str, Any],
    *,
    account_net_liq: float,
    regime_class: str,
    cash_available: float | None,
    universe_cache: dict[str, dict[str, pd.DataFrame]],
    top_per_strategy: int | None,
) -> tuple[str, pd.Timestamp, list[CandidateInput], str]:
    """Returns (setup_filename, signal_date, candidates, note)."""
    setup = row["setup"]
    try:
        spec_path = _find_spec_for(setup)
    except FileNotFoundError as exc:
        return (setup, None, [], str(exc))
    spec = yaml.safe_load(spec_path.read_text())
    kind = spec.get("kind")
    if kind not in KIND_TO_FORCE_TODAY:
        return (setup, None, [], f"kind {kind!r} not handled by manual_rebalance")

    params = _live_params_for(row, spec)
    benchmark = spec["universe"]["benchmark"]
    tickers = resolve_universe_tickers(spec)
    if benchmark not in tickers:
        tickers.append(benchmark)

    # Reuse fetched universe across kinds that share the same universe spec
    universe_key = spec["universe"].get("name") or f"_inline_{setup}"
    if universe_key not in universe_cache:
        print(f"  fetching universe {universe_key} ({len(tickers)} tickers)...", flush=True)
        universe_cache[universe_key] = _refresh_universe(tickers, force_refetch=False)
    universe_dfs = universe_cache[universe_key]

    if benchmark not in universe_dfs:
        return (setup, None, [], f"benchmark {benchmark!r} failed to load")

    signal_date = _last_bench_date(universe_dfs, benchmark)

    force_fn = KIND_TO_FORCE_TODAY[kind]
    eligible_ranked = force_fn(universe_dfs, params, signal_date)  # best-first list
    # Filter out benchmark + cap to top-N per strategy
    eligible_ranked = [t for t in eligible_ranked if t != benchmark]
    if top_per_strategy is not None and top_per_strategy > 0:
        eligible_ranked = eligible_ranked[:top_per_strategy]
    eligible_tickers = eligible_ranked

    # Build sized CandidateInputs inline using the kind-specific ATR
    # multiplier and the framework's 5% per-position concentration cap
    # (matches pipeline._check_track_limits — using the sizer's default 25%
    # would let every candidate hit the 5% rejection in place_candidate).
    atr_period = int(params.get("atr_period", 20))
    atr_mult = float(params.get("atr_stop_multiple", 2.0))
    limit_offset_pct = 0.002
    candidates: list[CandidateInput] = []
    for t in sorted(eligible_tickers):
        if t not in universe_dfs:
            continue
        tdf = universe_dfs[t]
        if len(tdf) == 0:
            continue
        pivot = float(tdf["Close"].iloc[-1])
        if pivot <= 0:
            continue
        atr = _compute_atr(tdf, period=atr_period)
        if atr is None or atr <= 0:
            continue
        stop_price = pivot - atr_mult * atr
        if stop_price >= pivot:
            continue
        limit_price = pivot * (1.0 + limit_offset_pct)
        try:
            sizer = position_sizer.compute(
                account=account_net_liq,
                entry_price=limit_price,
                atr=atr,
                setup_grade="B",
                regime_class=regime_class,
                atr_multiple=atr_mult,
                cash_available=cash_available,
                concentration_cap_pct=0.05,  # CLAUDE.md hard rule
            )
        except ValueError:
            continue
        shares = int(sizer.output.get("shares", 0))
        if shares <= 0:
            continue
        # Round to tick size — US equities are $0.01 (penny ticks).
        # Tiger rejects code=1200 ("tick size: 0.01") if limit/stop has
        # sub-cent precision.
        candidates.append(CandidateInput(
            ticker=t.upper(),
            setup_type=setup,
            setup_grade="B",
            pivot_price=pivot,
            limit_price=round(limit_price, 2),
            stop_price=round(stop_price, 2),
            target_price=None,
            shares=shares,
            sector_etf=_sector_etf_for(t),
        ))
    return (setup, signal_date, candidates, "")


# ---------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(prog="manual_rebalance")
    ap.add_argument("--place", action="store_true",
                    help="Actually place orders (default: dry-run only)")
    ap.add_argument("--top-per-strategy", type=int, default=1,
                    help="Cap top-N picks per ranked strategy (default: 1). "
                         "Skips the within-strategy flood (e.g. ts_momentum's 764).")
    ap.add_argument("--exclude", default="ts_momentum_liquid_us,connors_rsi2",
                    help="Comma-separated setups to skip entirely. "
                         "Default skips ts_momentum (no ranking, floods queue) "
                         "and connors_rsi2 (typically 0 signals).")
    ap.add_argument("--throttle-sec", type=float, default=1.5,
                    help="Seconds to sleep between place_candidate calls "
                         "(Tiger paper API rate-limits at ~60/min).")
    args = ap.parse_args()
    excluded = {s.strip() for s in (args.exclude or "").split(",") if s.strip()}

    print("# Manual rebalance — force signal_date = last bench bar")
    print(f"# Asof: {datetime.now().isoformat()}")
    print(f"# Mode: {'LIVE PLACEMENT' if args.place else 'DRY-RUN ONLY'}")
    print()

    # Tiger paper state
    print("[1/3] Tiger paper account summary...", flush=True)
    c = TigerClient()
    summary = c.account_summary().output
    net_liq = float(summary.get("net_liquidation") or 0.0)
    cash = float(summary.get("cash") or 0.0)
    print(f"  net_liquidation: ${net_liq:,.2f}")
    print(f"  cash:            ${cash:,.2f}")
    print()

    # Regime
    print("[2/3] SPY broad-market regime...", flush=True)
    passes_7 = tt_from_ticker("SPY", include_rs=False).output["trend_template_passes"]
    regime_class, regime_mult = classify_broad(passes_7)
    print(f"  passes: {passes_7}/7 · stage: {regime_class} · size_mult: {regime_mult}×")
    if regime_mult <= 0.0:
        print("  ⚠️ regime_mult is 0 — pipeline would HALT all entries. Aborting.")
        return
    print()

    # Per-row scan
    print("[3/3] Per-deployable-row eligibility @ last bench bar...", flush=True)
    print()
    data = config.load()
    rows = data.get("deployable", []) or []

    universe_cache: dict[str, dict[str, pd.DataFrame]] = {}
    all_candidates: list[tuple[str, CandidateInput]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        setup_name = row.get("setup", "")
        if setup_name in excluded:
            print(f"  {setup_name}: SKIPPED (--exclude)")
            continue
        setup, sig_date, cands, note = _candidates_for_row(
            row,
            account_net_liq=net_liq,
            regime_class=regime_class,
            cash_available=cash if cash > 0 else None,
            universe_cache=universe_cache,
            top_per_strategy=args.top_per_strategy,
        )
        sig_str = str(sig_date.date()) if sig_date is not None else "n/a"
        if note:
            print(f"  {setup}: SKIPPED — {note}")
            continue
        print(f"  {setup}: signal_date={sig_str} · "
              f"{len(cands)} candidates (top {args.top_per_strategy})")
        for cnd in cands:
            all_candidates.append((setup, cnd))
    print()

    if not all_candidates:
        print("No candidates from any deployable row. Nothing to place.")
        return

    # Order candidates by setup priority (heuristic: prefer ranked strategies
    # first because they have higher per-trade edge, leaving slots for them
    # when the 8-position cap binds).
    setup_priority = {
        "clenow_momentum_liquid_us": 1,
        "residual_momentum_liquid_us": 2,
        "xs_short_term_reversal_liquid_us": 3,
        "xs_short_term_reversal": 4,
        "ts_momentum_liquid_us": 5,
        "connors_rsi2": 6,
    }
    all_candidates.sort(key=lambda sc: (setup_priority.get(sc[0], 99), sc[1].ticker))

    # Run each through place_candidate
    print("## Pipeline results")
    print()
    print("| # | Setup | Ticker | Shares | Limit | Stop | Cost | Status | Detail |")
    print("|---|---|---|---|---|---|---|---|---|")
    import time as _time
    n = 0
    total_cost = 0.0
    n_placed = 0
    n_blocked = 0
    rejections: list[tuple[str, str, str]] = []
    for setup, cnd in all_candidates:
        n += 1
        if n > 1 and args.throttle_sec > 0:
            _time.sleep(args.throttle_sec)
        result = place_candidate(cnd, client=c, dry_run=not args.place)
        short_setup = setup.replace("_liquid_us", "_li").replace("_short_term_", "_st_")
        if result.status in ("dry_run", "placed"):
            n_placed += 1
            cost = cnd.shares * cnd.limit_price
            total_cost += cost
            emoji = "✅" if args.place else "🟡"
            detail = (f"order #{result.broker_order_id}" if args.place
                      else "would place")
        else:
            n_blocked += 1
            cost = 0.0
            emoji = "❌"
            detail = (result.reason or "")[:60]
            rejections.append((setup, cnd.ticker, result.reason or ""))
        print(f"| {n} | {short_setup} | {cnd.ticker} | {cnd.shares} | "
              f"${cnd.limit_price:.2f} | ${cnd.stop_price:.2f} | ${cost:,.0f} | "
              f"{emoji} {result.status} | {detail} |")

    print()
    print("## Summary")
    print(f"- Total candidates: {n}")
    print(f"- {'Placed' if args.place else 'Would place'}: **{n_placed}**")
    print(f"- Blocked: {n_blocked}")
    print(f"- Cost basis: ${total_cost:,.2f} ({(total_cost/net_liq*100) if net_liq>0 else 0:.1f}% of net liq)")
    print(f"- Cash after: ${cash - total_cost:,.2f}")

    if rejections:
        print()
        print(f"### Blocked ({len(rejections)})")
        by_reason: dict[str, int] = {}
        sample: dict[str, tuple[str, str]] = {}
        for setup, ticker, reason in rejections:
            key = reason.split(" (")[0][:80] if reason else "(no reason)"
            by_reason[key] = by_reason.get(key, 0) + 1
            sample.setdefault(key, (setup, ticker))
        for r_reason, k in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            setup, ticker = sample[r_reason]
            print(f"- **{k}×** `{r_reason}` (e.g. {setup}/{ticker})")

    if not args.place:
        print()
        print("---")
        print("This was a DRY-RUN. Re-run with `--place` to actually fire orders.")


if __name__ == "__main__":
    main()
