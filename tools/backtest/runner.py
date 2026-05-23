"""Phase 5 backtest orchestrator + CLI.

Pulls together :mod:`data_cache`, :mod:`setup_replay`, :mod:`simulator`,
:mod:`metrics`, and :mod:`walk_forward` into a single end-to-end run.

Phase 5.b additions:

* ``--trail`` flag (``fixed`` / ``ratchet`` / ``ma_trail``) selects the
  stop-trail policy applied to every simulated trade.
* ``--rolling`` flag switches from single 70/30 split to rolling
  walk-forward windows (``--is-years`` IS + ``--oos-years`` OOS, step
  ``--step-years``).
* Any setup registered in :data:`SETUP_REPLAY_REGISTRY` is selectable
  (SEPA-VCP / EP / Pullback-20SMA / RSI-Divergence / Resistance-Breakout).

CLI::

    uv run python -m tools.backtest.runner \\
        --setup SEPA-VCP \\
        --tickers SPY,QQQ,AAPL,MSFT,NVDA,GOOGL \\
        --start 2020-01-01 --end 2025-12-31 \\
        --trail ratchet
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date

from . import data_cache, metrics, pyramid_simulator, setup_replay, simulator, walk_forward
from .sell_aware import SellPolicy
from .trailing_stop import TrailConfig


def _format_pct(x: float) -> str:
    return f"{x:+.2f}%" if x else " 0.00%"


def _format_r(x: float) -> str:
    return f"{x:+.2f}R"


def _format_trade_stats(label: str, ts: metrics.TradeStats) -> list[str]:
    if ts.n_trades == 0:
        return [f"### {label}", "_no trades_", ""]
    return [
        f"### {label}",
        f"- Trades: **{ts.n_trades}** (wins {ts.n_wins} / losses {ts.n_losses} / breakeven {ts.n_breakeven})",
        f"- Win rate: **{ts.win_rate * 100:.1f}%**",
        f"- Avg winner: {_format_r(ts.avg_winner_r)} · Avg loser: {_format_r(ts.avg_loser_r)}",
        f"- Expectancy / trade: **{_format_r(ts.expectancy_r)}**",
        f"- Profit factor: **{ts.profit_factor:.2f}**",
        f"- Avg bars held: {ts.avg_bars_held:.1f}",
        "",
    ]


def _format_return_stats(rs: metrics.ReturnStats) -> list[str]:
    return [
        f"- Sharpe (annualised): **{rs.sharpe_annualised:.2f}**",
        f"- Sortino: {rs.sortino_annualised:.2f} · Calmar: {rs.calmar:.2f}",
        f"- Max drawdown: **{_format_pct(rs.max_drawdown_pct)}**",
        f"- Cumulative return: {_format_pct(rs.cumulative_return_pct)} (CAGR {_format_pct(rs.cagr_pct)})",
    ]


def _format_report(report: metrics.BacktestReport, header: str) -> str:
    lines: list[str] = [f"## {header}", ""]
    lines.extend(_format_trade_stats("Trade stats", report.trades))
    lines.append("### Return stats")
    lines.extend(_format_return_stats(report.returns))
    gate = "✅ PASSED" if report.deployment_gate_passed else "❌ FAILED"
    lines.append(f"- Deployment gate (Sharpe > 1.0 AND |DD| < 25% AND n ≥ 30): **{gate}**")
    if report.note:
        lines.append(f"- _Note: {report.note}_")
    lines.append("")
    if report.by_exit_reason:
        lines.append("### Exit reasons")
        for reason, count in sorted(report.by_exit_reason.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {reason}: {count}")
        lines.append("")
    if report.by_setup_grade:
        lines.append("### Per-grade trade stats")
        for grade, ts in sorted(report.by_setup_grade.items()):
            lines.append(
                f"- **{grade}**: n={ts.n_trades} · win_rate={ts.win_rate * 100:.0f}% · "
                f"expectancy={_format_r(ts.expectancy_r)} · PF={ts.profit_factor:.2f}"
            )
        lines.append("")
    return "\n".join(lines)


def _collect_signals_outcomes(
    setup: str,
    tickers: list[str],
    start: date,
    end: date,
    trail_config: TrailConfig,
    max_hold_days: int,
    target_r_multiple: float,
    force_refetch: bool,
    sell_policy: SellPolicy | None = None,
    pyramid_policy: pyramid_simulator.PyramidPolicy | None = None,
) -> tuple[
    list[setup_replay.TradeSignal],
    list[simulator.TradeOutcome],
    dict[str, int],
]:
    """Fetch + replay + simulate across all tickers.

    When ``pyramid_policy`` is set and enabled, uses
    :func:`pyramid_simulator.simulate_signals_pyramided` (mid-trade
    addon detection). Sell-policy is honored by the standard simulator
    (Phase 5.c addition) but not yet by the pyramid simulator —
    pyramid trades exit via stop/target/max-hold only in Phase 5.c.
    """
    if setup not in setup_replay.SETUP_REPLAY_REGISTRY:
        raise ValueError(
            f"unknown setup {setup!r}; known: {sorted(setup_replay.SETUP_REPLAY_REGISTRY)}"
        )
    replay_fn = setup_replay.SETUP_REPLAY_REGISTRY[setup]
    use_pyramid = pyramid_policy is not None and pyramid_policy.enabled

    all_signals: list[setup_replay.TradeSignal] = []
    all_outcomes: list[simulator.TradeOutcome] = []
    per_ticker: dict[str, int] = {}

    for ticker in tickers:
        try:
            data_cache.fetch(ticker, start=start, end=end, force_refetch=force_refetch)
        except Exception as exc:
            per_ticker[ticker] = 0
            print(f"# {ticker}: fetch failed — {exc}", flush=True)
            continue
        df = data_cache.load(ticker)
        df = walk_forward.trim_ohlcv(df, start, end)
        signals = replay_fn(
            df,
            ticker=ticker,
            max_hold_days=max_hold_days,
            target_r_multiple=target_r_multiple,
        )
        if use_pyramid:
            outcomes = pyramid_simulator.simulate_signals_pyramided(
                signals, df, trail_config=trail_config, pyramid_policy=pyramid_policy,
            )
        else:
            outcomes = simulator.simulate_signals(
                signals, df, trail_config=trail_config, sell_policy=sell_policy,
            )
        all_signals.extend(signals)
        all_outcomes.extend(outcomes)
        per_ticker[ticker] = len(outcomes)

    pairs = sorted(zip(all_signals, all_outcomes), key=lambda p: p[0].fill_date)
    all_signals = [p[0] for p in pairs]
    all_outcomes = [p[1] for p in pairs]
    return all_signals, all_outcomes, per_ticker


def run(
    setup: str,
    tickers: list[str],
    start: date,
    end: date,
    is_fraction: float = 0.70,
    trail: str = "fixed",
    trail_ma_period: int = 10,
    max_hold_days: int = 30,
    target_r_multiple: float = 2.0,
    risk_per_trade: float = 0.01,
    force_refetch: bool = False,
    sell_policy: SellPolicy | None = None,
    pyramid_policy: pyramid_simulator.PyramidPolicy | None = None,
) -> dict:
    """Single-split end-to-end run."""
    trail_config = TrailConfig(mode=trail, ma_period=trail_ma_period)
    spec = walk_forward.single_split(start=start, end=end, is_fraction=is_fraction)

    all_signals, all_outcomes, per_ticker = _collect_signals_outcomes(
        setup, tickers, start, end, trail_config, max_hold_days, target_r_multiple, force_refetch,
        sell_policy=sell_policy, pyramid_policy=pyramid_policy,
    )

    is_sigs, is_outs, oos_sigs, oos_outs = walk_forward.split_trades_by_window(
        all_signals, all_outcomes, spec
    )
    full_report = metrics.evaluate(all_outcomes, risk_per_trade=risk_per_trade)
    is_report = metrics.evaluate(is_outs, risk_per_trade=risk_per_trade)
    oos_report = metrics.evaluate(oos_outs, risk_per_trade=risk_per_trade)

    md = "\n".join(
        [
            f"# Phase 5 backtest — {setup}",
            "",
            f"- Tickers: {', '.join(tickers)}",
            f"- Period: {start} → {end}",
            f"- IS / OOS split at: {spec.in_sample_end} (IS fraction {is_fraction:.0%})",
            f"- Trail policy: **{trail}**" + (f" (MA period {trail_ma_period})" if trail == "ma_trail" else ""),
            f"- Max hold: {max_hold_days} bars · Target R-multiple: {target_r_multiple} · "
            f"Risk per trade: {risk_per_trade * 100:.1f}%",
            "",
            "## Signals per ticker",
            "",
        ]
        + [f"- {t}: {n} trade(s)" for t, n in per_ticker.items()]
        + [
            "",
            _format_report(full_report, "Full-period report"),
            _format_report(is_report, "In-sample (IS) report — tuning window"),
            _format_report(oos_report, "Out-of-sample (OOS) report — DEPLOYMENT GATE"),
            "## Doctrine verdict",
            "",
            f"OOS deployment gate: **{'PASSED' if oos_report.deployment_gate_passed else 'FAILED'}**",
            "",
            f"Per swing-risk-compliance-doctrine Phase 5 + walk-forward-analysis: a setup ships to live capital only when **OOS Sharpe > 1.0 AND |OOS max drawdown| < 25% AND OOS n ≥ 30 trades**.",
        ]
    )
    return {
        "markdown": md,
        "full": full_report,
        "in_sample": is_report,
        "out_of_sample": oos_report,
        "per_ticker_counts": per_ticker,
        "deployment_gate_passed": oos_report.deployment_gate_passed,
        "spec": asdict(spec),
    }


def run_rolling(
    setup: str,
    tickers: list[str],
    start: date,
    end: date,
    is_years: int = 3,
    oos_years: int = 1,
    step_years: int = 1,
    trail: str = "fixed",
    trail_ma_period: int = 10,
    max_hold_days: int = 30,
    target_r_multiple: float = 2.0,
    risk_per_trade: float = 0.01,
    force_refetch: bool = False,
    sell_policy: SellPolicy | None = None,
    pyramid_policy: pyramid_simulator.PyramidPolicy | None = None,
) -> dict:
    """Rolling walk-forward end-to-end run.

    Returns aggregated OOS metrics across all windows, plus per-window
    breakdown. The deployment-gate verdict is computed on the
    **concatenated** OOS outcomes across all windows.
    """
    trail_config = TrailConfig(mode=trail, ma_period=trail_ma_period)
    specs = walk_forward.rolling_splits(
        start=start, end=end, is_years=is_years, oos_years=oos_years, step_years=step_years
    )
    if not specs:
        raise ValueError(
            f"no rolling windows fit start={start} end={end} "
            f"with IS={is_years}y OOS={oos_years}y"
        )

    all_signals, all_outcomes, per_ticker = _collect_signals_outcomes(
        setup, tickers, start, end, trail_config, max_hold_days, target_r_multiple, force_refetch,
        sell_policy=sell_policy, pyramid_policy=pyramid_policy,
    )

    window_reports: list[dict] = []
    concat_oos_outs: list[simulator.TradeOutcome] = []
    for spec in specs:
        _is_sigs, is_outs, _oos_sigs, oos_outs = walk_forward.split_trades_by_window(
            all_signals, all_outcomes, spec
        )
        oos_report = metrics.evaluate(oos_outs, risk_per_trade=risk_per_trade)
        is_report = metrics.evaluate(is_outs, risk_per_trade=risk_per_trade)
        window_reports.append(
            {
                "spec": asdict(spec),
                "is_n": is_report.trades.n_trades,
                "is_expectancy": is_report.trades.expectancy_r,
                "oos_n": oos_report.trades.n_trades,
                "oos_expectancy": oos_report.trades.expectancy_r,
                "oos_sharpe": oos_report.returns.sharpe_annualised,
                "oos_max_dd_pct": oos_report.returns.max_drawdown_pct,
                "oos_gate": oos_report.deployment_gate_passed,
            }
        )
        concat_oos_outs.extend(oos_outs)

    aggregate_oos = metrics.evaluate(concat_oos_outs, risk_per_trade=risk_per_trade)

    md_lines: list[str] = [
        f"# Phase 5 backtest — {setup} — rolling walk-forward",
        "",
        f"- Tickers: {', '.join(tickers)}",
        f"- Period: {start} → {end}",
        f"- IS years: {is_years} · OOS years: {oos_years} · Step: {step_years}y",
        f"- Windows: {len(specs)}",
        f"- Trail policy: **{trail}**" + (f" (MA period {trail_ma_period})" if trail == "ma_trail" else ""),
        f"- Max hold: {max_hold_days} bars · Target R-multiple: {target_r_multiple} · Risk per trade: {risk_per_trade * 100:.1f}%",
        "",
        "## Signals per ticker",
        "",
    ]
    md_lines.extend(f"- {t}: {n} trade(s)" for t, n in per_ticker.items())
    md_lines.extend(["", "## Per-window OOS breakdown", "", "| Window IS | Window OOS | IS n | OOS n | OOS Sharpe | OOS DD | Gate |", "|---|---|---|---|---|---|---|"])
    for wr in window_reports:
        s = wr["spec"]
        md_lines.append(
            f"| {s['in_sample_start']}..{s['in_sample_end']} | "
            f"{s['in_sample_end']}..{s['out_of_sample_end']} | "
            f"{wr['is_n']} | {wr['oos_n']} | "
            f"{wr['oos_sharpe']:.2f} | {wr['oos_max_dd_pct']:+.2f}% | "
            f"{'✅' if wr['oos_gate'] else '❌'} |"
        )
    md_lines.extend(
        [
            "",
            _format_report(aggregate_oos, "Aggregated OOS across all windows — DEPLOYMENT GATE"),
            "## Doctrine verdict",
            "",
            f"Aggregated OOS deployment gate: **{'PASSED' if aggregate_oos.deployment_gate_passed else 'FAILED'}**",
            "",
            "Per swing-risk-compliance-doctrine Phase 5 + walk-forward-analysis: a setup ships to live capital only when the **aggregated OOS** Sharpe > 1.0 AND |max drawdown| < 25% AND n ≥ 30 trades. Individual-window failures are warnings; the aggregated gate is what governs deployment.",
        ]
    )
    return {
        "markdown": "\n".join(md_lines),
        "aggregate_oos": aggregate_oos,
        "windows": window_reports,
        "per_ticker_counts": per_ticker,
        "deployment_gate_passed": aggregate_oos.deployment_gate_passed,
    }


def main() -> None:
    p = argparse.ArgumentParser(prog="tools.backtest.runner")
    p.add_argument("--setup", default="SEPA-VCP", choices=sorted(setup_replay.SETUP_REPLAY_REGISTRY))
    p.add_argument("--tickers", required=True, help="Comma-separated, e.g. SPY,QQQ,AAPL")
    p.add_argument("--start", required=True, help="ISO date")
    p.add_argument("--end", required=True, help="ISO date")
    p.add_argument("--trail", default="fixed", choices=["fixed", "ratchet", "ma_trail"])
    p.add_argument("--trail-ma-period", type=int, default=10)
    p.add_argument("--max-hold-days", type=int, default=30)
    p.add_argument("--target-r-multiple", type=float, default=2.0)
    p.add_argument("--risk-per-trade", type=float, default=0.01)
    p.add_argument("--force-refetch", action="store_true")

    p.add_argument("--rolling", action="store_true",
                   help="Use rolling walk-forward windows instead of single IS/OOS split")
    p.add_argument("--is-fraction", type=float, default=0.70,
                   help="Single-split mode: IS/OOS fraction (default 0.70)")
    p.add_argument("--is-years", type=int, default=3,
                   help="Rolling mode: IS window length in years")
    p.add_argument("--oos-years", type=int, default=1,
                   help="Rolling mode: OOS window length in years")
    p.add_argument("--step-years", type=int, default=1,
                   help="Rolling mode: window step in years")

    # Phase 5.c flags
    p.add_argument("--pyramid", action="store_true",
                   help="Enable Anchor-and-Pyramid multi-leg simulation (Phase 5.c). "
                        "Mid-trade Momentum Burst adds ADD-ON #1; Day-7 milestone adds ADD-ON #2 "
                        "for Super Swan / Golden EP grades.")
    p.add_argument("--addon-1-window", type=int, default=10,
                   help="Pyramid mode: max bars after starter to look for ADD-ON #1 (default 10)")
    p.add_argument("--regime-class", default="stage_2_confirmed",
                   choices=["stage_2_confirmed", "stage_2_weakening",
                            "stage_3_transitional", "stage_4"],
                   help="Ambient regime class for pyramid + sell-aware composer (Phase 5.c)")
    p.add_argument("--sell-aware", action="store_true",
                   help="Enable per-bar sell-discipline composer exits (Phase 5.c). "
                        "Climax-top, violations, base-stage, sell-into-strength signals "
                        "from sell_decision composer exit when action != 'hold'.")
    p.add_argument("--sell-grace-period", type=int, default=3,
                   help="sell-aware mode: bars after entry to suppress sell-decision (default 3)")

    args = p.parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    sell_policy = (
        SellPolicy(enabled=True, regime_class=args.regime_class,
                   grace_period_bars=args.sell_grace_period)
        if args.sell_aware else None
    )
    pyramid_policy = (
        pyramid_simulator.PyramidPolicy(
            enabled=True,
            addon_1_max_window_bars=args.addon_1_window,
            regime_class=args.regime_class,
        )
        if args.pyramid else None
    )
    if args.pyramid and args.sell_aware:
        print(
            "# WARNING: --pyramid + --sell-aware combined is Phase 5.d; "
            "pyramid simulator currently ignores sell_policy. Continuing with pyramid only.",
            flush=True,
        )

    if args.rolling:
        result = run_rolling(
            setup=args.setup,
            tickers=tickers,
            start=date.fromisoformat(args.start),
            end=date.fromisoformat(args.end),
            is_years=args.is_years,
            oos_years=args.oos_years,
            step_years=args.step_years,
            trail=args.trail,
            trail_ma_period=args.trail_ma_period,
            max_hold_days=args.max_hold_days,
            target_r_multiple=args.target_r_multiple,
            risk_per_trade=args.risk_per_trade,
            force_refetch=args.force_refetch,
            sell_policy=sell_policy,
            pyramid_policy=pyramid_policy,
        )
    else:
        result = run(
            setup=args.setup,
            tickers=tickers,
            start=date.fromisoformat(args.start),
            end=date.fromisoformat(args.end),
            is_fraction=args.is_fraction,
            trail=args.trail,
            trail_ma_period=args.trail_ma_period,
            max_hold_days=args.max_hold_days,
            target_r_multiple=args.target_r_multiple,
            risk_per_trade=args.risk_per_trade,
            force_refetch=args.force_refetch,
            sell_policy=sell_policy,
            pyramid_policy=pyramid_policy,
        )
    print(result["markdown"])


if __name__ == "__main__":
    main()
