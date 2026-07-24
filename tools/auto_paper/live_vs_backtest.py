"""Live-vs-backtest tracking report (cherry-pick Batch B, B3).

Standing answer to "is the live/paper sleeve tracking the backtest
distribution?", built from three parts:

1. **Daily sleeve curve reconstruction.** No daily NAV series is persisted
   for the paper-auto sleeve, so the report reconstructs one from ledger
   entry fills + cached daily closes: each trade contributes
   ``shares x (close_t - close_{t-1})`` per holding day (entry day vs fill
   price, exit day vs exit price). Sharpe/Sortino/PSR on the resulting
   daily return series are INVARIANT to the ``base_equity`` scaling (mean
   and std scale together); only %-drawdown and the cone's %-units depend
   on it — stated on the report.

2. **Probabilistic Sharpe Ratio** (tools.backtest.sharpe_stats, Bailey &
   Lopez de Prado 2012/2014) of each setup's live daily returns against
   that setup's backtest Sharpe benchmark: the roster row's
   ``live_benchmark_sharpe`` (net-of-cost OOS aggregate) when present,
   falling back to ``rolling_agg_sharpe`` (zero-cost — labeled as such).
   PSR = P(true live SR > backtest benchmark). Small T makes this
   deliberately weak evidence early — the report prints T so nobody reads
   false confidence into 20 trading days.

3. **Expectation cone** (pyfolio-reloaded split-report pattern): expected
   cumulative return path from the BACKTEST Sharpe (shape) x the LIVE
   realized vol (scale):  E[cum_t] = SR_bt x sigma_live x t/252, band
   +/- z x sigma_live x sqrt(t/252). Using live vol rather than backtest
   vol is deliberate — the roster records backtest Sharpe/MDD but not vol,
   and the hybrid keeps the cone in the sleeve's own units. Documented
   here so nobody mistakes it for a full backtest-distribution cone.

Corrupt-ledger guard: any trade whose notional exceeds
``MAX_NOTIONAL_FRAC`` of base equity is EXCLUDED from the reconstruction
and listed in the report (motivating case: the 2026-07-24 NFLX ledger
rewrite — 29,760 fictional shares must not fabricate sleeve P&L).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from ..backtest import sharpe_stats

_ROOT = Path(__file__).resolve().parents[2]
LEDGER_DIR = _ROOT / "ledgers" / "paper-auto"
ROSTER_PATH = _ROOT / "tools" / "deployable_setups.yml"

TRADING_DAYS = 252
MAX_NOTIONAL_FRAC = 0.10   # > 10% of base equity = corrupt row, exclude + report
_EXCLUDED_STATES = {"submitted", "closed_unfilled"}
_ROSTER_SECTIONS = (
    "deployable",
    "parked_by_concurrent_cap_reveals_weak_edge",
    "parked_by_tightened_gate",
)


@dataclass
class SleeveTrade:
    ticker: str
    setup: Optional[str]
    entry_date: date
    entry_price: float
    shares: float
    exit_date: Optional[date]
    exit_price: Optional[float]
    state: Optional[str]
    caveats: list[str] = field(default_factory=list)


def _parse_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    s = str(v)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def trades_from_ledgers(ledger_dir: Optional[Path] = None) -> tuple[list[SleeveTrade], list[str]]:
    """Scan paper-auto ledgers into sleeve trades. Returns (trades, notes) —
    notes records every skip/fallback so the report can print them."""
    d = Path(ledger_dir) if ledger_dir else LEDGER_DIR
    trades: list[SleeveTrade] = []
    notes: list[str] = []
    for p in sorted(d.glob("*.yml")):
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            notes.append(f"{p.name}: unreadable YAML — skipped")
            continue
        meta = doc.get("meta") or {}
        state = (meta.get("state") or "").strip().lower()
        if state in _EXCLUDED_STATES:
            continue
        ticker = meta.get("ticker") or p.stem
        ps = doc.get("position_state") or {}
        starter = ps.get("starter") or {}
        entry_date = _parse_date(starter.get("fill_date"))
        try:
            entry_price = float(starter.get("fill_price"))
            shares = float(starter.get("shares"))
        except (TypeError, ValueError):
            entry_price = shares = 0.0
        if not entry_date or entry_price <= 0 or shares <= 0:
            notes.append(f"{ticker}: no usable entry fill — skipped")
            continue
        caveats: list[str] = []
        exit_price = ps.get("exit_price")
        exit_price = float(exit_price) if exit_price is not None else None
        exit_date = _parse_date(ps.get("exit_date"))
        if state == "closed" and exit_price is None:
            notes.append(f"{ticker}: closed without exit_price — skipped (mirrors performance.py)")
            continue
        if exit_price is not None and exit_date is None:
            exit_date = _parse_date(meta.get("updated_at"))
            caveats.append("exit_date approximated from meta.updated_at")
        trades.append(SleeveTrade(
            ticker=ticker, setup=(doc.get("setup_classification") or {}).get("type"),
            entry_date=entry_date, entry_price=entry_price, shares=shares,
            exit_date=exit_date, exit_price=exit_price, state=state or None,
            caveats=caveats,
        ))
    return trades, notes


def _default_price_loader(ticker: str):
    from ..backtest import data_cache
    try:
        return data_cache.load(ticker)
    except Exception:
        return None


def reconstruct_daily_pnl(
    trades: list[SleeveTrade],
    *,
    base_equity: float,
    asof: date,
    price_loader: Optional[Callable[[str], Any]] = None,
):
    """Daily P&L per setup. Returns (DataFrame[date x setup + '_total'],
    excluded) where excluded lists (trade, reason) pairs — the corrupt-ledger
    guard and missing-price-history cases land there, never silently."""
    import pandas as pd

    price_loader = price_loader or _default_price_loader
    excluded: list[tuple[SleeveTrade, str]] = []
    per_trade: list[tuple[SleeveTrade, "pd.Series"]] = []
    for t in trades:
        if t.entry_price * t.shares > MAX_NOTIONAL_FRAC * base_equity:
            excluded.append((t, (
                f"notional ${t.entry_price * t.shares:,.0f} > "
                f"{MAX_NOTIONAL_FRAC:.0%} of base equity — corrupt-ledger guard"
            )))
            continue
        df = price_loader(t.ticker)
        if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
            excluded.append((t, "no cached price history"))
            continue
        closes = df["Close"].astype(float)
        closes.index = pd.to_datetime(closes.index).date
        end = min(t.exit_date or asof, asof)
        days = sorted(d for d in closes.index if t.entry_date <= d <= end)
        if not days:
            excluded.append((t, "no bars inside holding window"))
            continue
        pnl = {}
        prev = t.entry_price
        for d_ in days:
            mark = float(closes.loc[d_])
            if t.exit_date is not None and d_ == end and t.exit_price is not None:
                mark = t.exit_price
            pnl[d_] = (mark - prev) * t.shares
            prev = mark
        per_trade.append((t, pd.Series(pnl)))
    if not per_trade:
        return pd.DataFrame(), excluded
    frame: dict[str, "pd.Series"] = {}
    for t, s in per_trade:
        key = t.setup or "unclassified"
        frame[key] = s.add(frame[key], fill_value=0.0) if key in frame else s
    df = pd.DataFrame(frame).fillna(0.0).sort_index()
    df["_total"] = df.sum(axis=1)
    return df, excluded


# ---------------------------------------------------------------------------
# Roster benchmarks
# ---------------------------------------------------------------------------

def load_benchmarks(roster_path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """setup name -> {sharpe, source} from deployable_setups.yml (all
    sections — live trades may reference since-retired setups)."""
    p = Path(roster_path) if roster_path else ROSTER_PATH
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for section in _ROSTER_SECTIONS:
        for row in doc.get(section) or []:
            name = row.get("setup")
            if not name:
                continue
            if row.get("live_benchmark_sharpe") is not None:
                out[name] = {"sharpe": float(row["live_benchmark_sharpe"]),
                             "source": "live_benchmark_sharpe (net-of-cost)"}
            elif row.get("rolling_agg_sharpe") is not None:
                out[name] = {"sharpe": float(row["rolling_agg_sharpe"]),
                             "source": "rolling_agg_sharpe (ZERO-COST)"}
    return out


# ---------------------------------------------------------------------------
# Stats + report
# ---------------------------------------------------------------------------

def _curve_stats(returns) -> dict[str, Any]:
    import numpy as np
    r = returns.dropna()
    n = len(r)
    out: dict[str, Any] = {"n_days": n}
    if n < 4:
        out["note"] = "fewer than 4 daily observations — stats withheld"
        return out
    mean, std = float(r.mean()), float(r.std(ddof=1))
    downside = float(r[r < 0].std(ddof=1)) if (r < 0).any() else float("nan")
    cum = (1 + r).cumprod()
    peak = cum.cummax()
    out.update(
        cum_return_pct=float(cum.iloc[-1] - 1) * 100,
        sharpe_ann=(mean / std) * np.sqrt(TRADING_DAYS) if std > 0 else None,
        sortino_ann=(mean / downside) * np.sqrt(TRADING_DAYS)
        if downside and downside > 0 else None,
        max_dd_pct=float(((cum - peak) / peak).min()) * 100,
        vol_ann_pct=std * np.sqrt(TRADING_DAYS) * 100,
    )
    return out


def compute_report(
    *,
    base_equity: float,
    asof: Optional[date] = None,
    ledger_dir: Optional[Path] = None,
    roster_path: Optional[Path] = None,
    price_loader: Optional[Callable[[str], Any]] = None,
) -> dict[str, Any]:
    asof = asof or date.today()
    trades, notes = trades_from_ledgers(ledger_dir)
    pnl, excluded = reconstruct_daily_pnl(
        trades, base_equity=base_equity, asof=asof, price_loader=price_loader,
    )
    benchmarks = load_benchmarks(roster_path)
    report: dict[str, Any] = {
        "asof": asof.isoformat(), "base_equity": base_equity,
        "n_trades": len(trades), "notes": notes,
        "excluded": [
            {"ticker": t.ticker, "setup": t.setup, "reason": reason}
            for t, reason in excluded
        ],
        "setups": [], "sleeve": {}, "cone": [],
    }
    if pnl.empty:
        report["sleeve"] = {"note": "no reconstructable trades"}
        return report

    returns_total = pnl["_total"] / base_equity
    sleeve_stats = _curve_stats(returns_total)
    weighted_bench_num = weighted_bench_den = 0.0

    for setup_name in [c for c in pnl.columns if c != "_total"]:
        r = pnl[setup_name] / base_equity
        active = r[r != 0.0]
        stats = _curve_stats(r)
        row: dict[str, Any] = {"setup": setup_name, **stats,
                               "n_active_days": len(active)}
        bench = benchmarks.get(setup_name)
        if bench is not None:
            row["benchmark_sharpe_ann"] = bench["sharpe"]
            row["benchmark_source"] = bench["source"]
            notional = sum(
                t.entry_price * t.shares for t in trades if (t.setup or "unclassified") == setup_name
            )
            weighted_bench_num += bench["sharpe"] * notional
            weighted_bench_den += notional
            try:
                m = sharpe_stats.sharpe_moments(list(r.dropna()))
                row["psr_vs_backtest"] = sharpe_stats.psr(
                    m["sr"],
                    sharpe_stats.annualized_to_per_period(bench["sharpe"]),
                    m["n"], m["skew"], m["kurt_raw"],
                )
            except ValueError as exc:
                row["psr_note"] = str(exc)
        else:
            row["benchmark_note"] = "no roster row — PSR not computable"
        report["setups"].append(row)

    report["sleeve"] = sleeve_stats
    if weighted_bench_den > 0 and sleeve_stats.get("n_days", 0) >= 4:
        bench_sleeve = weighted_bench_num / weighted_bench_den
        report["sleeve"]["benchmark_sharpe_ann"] = bench_sleeve
        report["sleeve"]["benchmark_note"] = "notional-weighted across setup benchmarks"
        try:
            m = sharpe_stats.sharpe_moments(list(returns_total.dropna()))
            report["sleeve"]["psr_vs_backtest"] = sharpe_stats.psr(
                m["sr"], sharpe_stats.annualized_to_per_period(bench_sleeve),
                m["n"], m["skew"], m["kurt_raw"],
            )
        except ValueError as exc:
            report["sleeve"]["psr_note"] = str(exc)
        # Expectation cone: backtest Sharpe (shape) x live realized vol (scale).
        vol_ann = (sleeve_stats.get("vol_ann_pct") or 0.0) / 100.0
        if vol_ann > 0:
            import math
            t_now = sleeve_stats["n_days"]
            actual_cum = sleeve_stats["cum_return_pct"] / 100.0
            for t_days in (5, 10, 21, 42, 63, 126, 252):
                t_frac = t_days / TRADING_DAYS
                expected = bench_sleeve * vol_ann * t_frac
                band = vol_ann * math.sqrt(t_frac)
                row = {
                    "t_days": t_days,
                    "expected_pct": expected * 100,
                    "lo_1sd_pct": (expected - band) * 100,
                    "hi_1sd_pct": (expected + band) * 100,
                    "lo_2sd_pct": (expected - 2 * band) * 100,
                    "hi_2sd_pct": (expected + 2 * band) * 100,
                }
                if t_days == min(
                    (5, 10, 21, 42, 63, 126, 252),
                    key=lambda x: abs(x - t_now),
                ):
                    row["actual_pct"] = actual_cum * 100
                    row["z_score"] = (
                        (actual_cum - bench_sleeve * vol_ann * t_now / TRADING_DAYS)
                        / (vol_ann * math.sqrt(t_now / TRADING_DAYS))
                    )
                report["cone"].append(row)
    return report


def render_markdown(report: dict[str, Any]) -> str:
    def _f(v, fmt="{:.2f}"):
        return fmt.format(v) if isinstance(v, (int, float)) else "—"
    L = [
        "## Live-vs-backtest tracking report (B3)",
        "",
        f"- asof: {report['asof']} · base equity ${report['base_equity']:,.0f} "
        "(Sharpe/Sortino/PSR are base-invariant; %-figures are not)",
        f"- trades reconstructed: {report['n_trades']}"
        + (f" · excluded: {len(report['excluded'])}" if report["excluded"] else ""),
        "",
        "### Sleeve (all setups, daily mark-to-market)",
        "",
    ]
    s = report["sleeve"]
    if "note" in s:
        L.append(f"- {s['note']}")
    else:
        L += [
            f"- T = {s['n_days']} trading days — PSR at this sample size is weak "
            "evidence by construction; it firms up as T grows",
            f"- cum return {_f(s.get('cum_return_pct'))}% · Sharpe(ann) "
            f"{_f(s.get('sharpe_ann'))} · Sortino(ann) {_f(s.get('sortino_ann'))} · "
            f"maxDD {_f(s.get('max_dd_pct'))}% · vol(ann) {_f(s.get('vol_ann_pct'))}%",
        ]
        if "psr_vs_backtest" in s:
            L.append(
                f"- **PSR vs backtest benchmark {_f(s.get('benchmark_sharpe_ann'))} "
                f"(ann, {s.get('benchmark_note')})**: "
                f"**{s['psr_vs_backtest']:.1%}** = P(true live Sharpe exceeds benchmark)"
            )
    L += ["", "### Per setup", "",
          "| setup | T | active | cum % | Sharpe | benchmark (ann) | PSR | note |",
          "|---|---|---|---|---|---|---|---|"]
    for r in report["setups"]:
        L.append(
            f"| {r['setup']} | {r.get('n_days', '—')} | {r.get('n_active_days', '—')} "
            f"| {_f(r.get('cum_return_pct'))} | {_f(r.get('sharpe_ann'))} "
            f"| {_f(r.get('benchmark_sharpe_ann'))} "
            f"({(r.get('benchmark_source') or '—').split(' ')[0]}) "
            f"| {_f(r.get('psr_vs_backtest'), '{:.1%}')} "
            f"| {r.get('psr_note') or r.get('benchmark_note') or ''} |"
        )
    if report["cone"]:
        L += ["", "### Expectation cone (backtest Sharpe x live realized vol)", "",
              "| horizon (days) | -2σ % | -1σ % | expected % | +1σ % | +2σ % | actual % | z |",
              "|---|---|---|---|---|---|---|---|"]
        for c in report["cone"]:
            L.append(
                f"| {c['t_days']} | {_f(c['lo_2sd_pct'])} | {_f(c['lo_1sd_pct'])} "
                f"| {_f(c['expected_pct'])} | {_f(c['hi_1sd_pct'])} | {_f(c['hi_2sd_pct'])} "
                f"| {_f(c.get('actual_pct'))} | {_f(c.get('z_score'))} |"
            )
    if report["excluded"]:
        L += ["", "### Excluded trades (guards — investigate, never silently drop)", ""]
        for e in report["excluded"]:
            L.append(f"- {e['ticker']} ({e['setup']}): {e['reason']}")
    if report["notes"]:
        L += ["", "### Reconstruction notes", ""]
        L += [f"- {n}" for n in report["notes"]]
    return "\n".join(L) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Live-vs-backtest tracking report (B3)")
    ap.add_argument("--base-equity", type=float, default=1_000_000.0,
                    help="sleeve base equity for %% scaling (Sharpe/PSR are invariant to it)")
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--out", default=None, help="write markdown here instead of stdout")
    args = ap.parse_args(argv)
    asof = _parse_date(args.asof) if args.asof else None
    report = compute_report(base_equity=args.base_equity, asof=asof)
    md = render_markdown(report)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
