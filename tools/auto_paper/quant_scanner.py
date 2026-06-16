"""Quant-scanner — generate live CandidateInputs from KIND_REGISTRY setups.

Bridges the backtest-only KIND_REGISTRY family (dual_ma_trend_following,
xs_short_term_reversal, connors_rsi2, clenow_momentum) into the live
auto-paper pipeline.

Flow (called from ``/auto-paper`` at 9:35 AM ET):

  1. Read ``tools/deployable_setups.yml`` for the live deployable list.
  2. For each row whose ``setup`` is in :data:`KIND_REGISTRY`:
     a. Load the spec YAML at ``tools/quant_strategies/<setup>.yml``.
     b. Extract live params from the row's ``deployable_params:`` block
        (or fall back to the first grid combo if absent).
     c. Refresh OHLCV via :mod:`tools.backtest.data_cache` for every
        ticker in the spec's universe.
     d. Run the kind's ``precompute()`` over the universe.
     e. Detect tickers eligible on the LAST AVAILABLE trading day
        (= yesterday's close in live; signal day is yesterday, fill
        day is today).
     f. For each eligible ticker, compose a :class:`CandidateInput`
        with limit price = yesterday_close × (1 + ``limit_offset_pct``)
        and stop price = yesterday_close − ``atr_stop_multiple`` × ATR.
  3. Return the combined list.

The caller (``/auto-paper`` or any test) is then responsible for
threading each CandidateInput through
:func:`tools.auto_paper.pipeline.place_candidate`, which applies the
deployable-setup filter (defensive), the paper-auto-track-only hard
rules (5% / 20% / 8 / 15%), the position-sizer, and the broker call.

The scanner does NOT place orders itself and does NOT mutate the
ledger / positions.json — that is the pipeline's job, called by the
caller.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import yaml

from tools.auto_paper import config, entry_pricing
from tools.auto_paper.pipeline import CandidateInput
from tools.backtest import data_cache
from tools.contract import TraceEntry
from tools.quant_strategies._kinds import KIND_REGISTRY
from tools.quant_strategies._universe import resolve_universe_tickers
from tools import position_sizer


SPEC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tools",
    "quant_strategies",
)


@dataclass
class ScannerReport:
    """Per-setup record of what the scanner found."""
    setup: str
    spec_path: str
    eligible_tickers: list[str]
    candidates: list[CandidateInput]
    signal_date: Optional[date]
    note: str = ""


# ---------------------------------------------------------------- spec loading


def _find_spec_for(setup: str) -> Path:
    """Resolve ``setup`` → path to its quant_strategies YAML spec."""
    p = Path(SPEC_DIR) / f"{setup}.yml"
    if not p.is_file():
        raise FileNotFoundError(
            f"no quant_strategies spec for {setup!r} at {p}"
        )
    return p


def _flatten_grid_to_first_combo(params: dict[str, Any]) -> dict[str, Any]:
    """Take the spec's ``params:`` block and return a single param dict.

    If a value is a list (grid), use the first element. If a value is a
    scalar, use it as-is. Used as the fall-back when the deployable_setups
    row does not specify ``deployable_params``.
    """
    out: dict[str, Any] = {}
    for k, v in params.items():
        if isinstance(v, list):
            out[k] = v[0] if v else None
        else:
            out[k] = v
    return out


def _live_params_for(row: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Resolve the params dict to use for live signal generation.

    Preference order:
      1. ``row["deployable_params"]`` — explicit live config
      2. First combo of each grid in ``spec["params"]``
    """
    params = _flatten_grid_to_first_combo(spec.get("params", {}))
    explicit = row.get("deployable_params", {}) or {}
    params.update(explicit)
    # Always thread benchmark from the spec.
    params.setdefault("benchmark", spec["universe"]["benchmark"])
    return params


# ---------------------------------------------------------------- data refresh


def _benchmark_cache_age_hours(benchmark: str = "SPY") -> Optional[float]:
    """Return the cache file's age in hours, or None if uncached / unreadable.

    Used to decide whether the cache is stale enough that a force-refetch is
    warranted. Reads the ``fetched_at`` timestamp from the cache meta sidecar
    (written by :mod:`tools.backtest.data_cache`) rather than the cache file's
    filesystem mtime — meta is the contract; mtime can drift on file moves.
    """
    try:
        entry = data_cache.info(benchmark)
    except Exception:
        return None
    if entry is None:
        return None
    fetched_at_str = entry.fetched_at
    if not fetched_at_str or fetched_at_str == "unknown":
        return None
    try:
        # Parse "2026-05-22T13:45:30+00:00" → aware datetime
        fetched_at = datetime.fromisoformat(fetched_at_str)
    except (TypeError, ValueError):
        return None
    now = datetime.now(timezone.utc)
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return (now - fetched_at).total_seconds() / 3600.0


def _refresh_universe(
    tickers: list[str],
    *,
    start_date: Optional[date] = None,
    lookback_days: int = 400,
    force_refetch: bool = False,
    cache_max_age_hours: float = 18.0,
    benchmark: str = "SPY",
) -> dict[str, pd.DataFrame]:
    """Pull / refresh OHLCV for every ticker; return ``{ticker: df}``.

    Bug-2 fix (2026-05-26): the LIVE rebalance schedule is computed by each
    kind's ``precompute()`` as ``range(start_idx, n, step)`` over the loaded
    bench_dates. With a moving ``lookback_days`` window, the schedule slides
    daily and no longer matches the BACKTEST schedule (which anchors at
    ``spec.period.start``). Callers should pass ``start_date`` =
    ``date.fromisoformat(spec["period"]["start"])`` so the live schedule
    matches the backtest's exactly — same rebalance dates, same selections.

    Cache-staleness fix (2026-05-28): :func:`tools.backtest.data_cache.fetch`
    returns the existing cache without checking ``fetched_at``, so a stale
    cache produces silently stale signals. This function now inspects the
    benchmark ticker's cache age and, if older than ``cache_max_age_hours``,
    promotes ``force_refetch`` to True for the entire universe. The benchmark
    is the canonical clock — if SPY's cache is stale, every other ticker's
    cache is also stale (they were all written at the same cron tick).

    Args:
        tickers: list of tickers to fetch.
        start_date: fixed history anchor (preferred). Wins over lookback_days
            when both are set.
        lookback_days: fallback when ``start_date`` is None. ``400`` gives
            ~16 months — enough for a 200d SMA + 100d momentum lookback for
            kinds that DON'T have a per-rebalance schedule (e.g. connors_rsi2,
            which is per-day-eligible — no schedule drift to align).
        force_refetch: re-pull from yfinance even if cached. The
            ``cache_max_age_hours`` check can flip this to True automatically.
        cache_max_age_hours: if the benchmark cache's ``fetched_at`` is older
            than this many hours, force a universe-wide refetch. Default 18h
            ≈ one trading day — a daily cron at the same wall-clock time will
            always see ~24h-old cache and refresh; intraday re-runs inside
            the same session see a fresh cache and skip the refetch. Pass
            ``float("inf")`` to disable the auto-refresh.
        benchmark: ticker used as the staleness clock. Default SPY.
    """
    if not force_refetch and cache_max_age_hours != float("inf"):
        age_h = _benchmark_cache_age_hours(benchmark)
        if age_h is None or age_h > cache_max_age_hours:
            print(
                f"# quant_scanner: benchmark {benchmark} cache age="
                f"{('unknown' if age_h is None else f'{age_h:.1f}h')} "
                f"exceeds max_age={cache_max_age_hours:.1f}h — "
                f"force_refetch=True for universe",
                flush=True,
            )
            force_refetch = True
    end = date.today()
    if start_date is not None:
        start = start_date
    else:
        start = end - timedelta(days=lookback_days)
    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            data_cache.fetch(t, start=start, end=end, force_refetch=force_refetch)
            out[t] = data_cache.load(t)
        except Exception as exc:
            # Skip on fetch failure; surface in report.note via the caller.
            print(f"# quant_scanner: skipped {t} — fetch failed: {exc}", flush=True)
            continue
    return out


# ---------------------------------------------------------------- eligibility


def _last_trading_date(bench_df: pd.DataFrame) -> Optional[pd.Timestamp]:
    """The most recent date in the benchmark's index."""
    if len(bench_df) == 0:
        return None
    return bench_df.index[-1]


def _eligible_tickers_on(state: Any, day: pd.Timestamp) -> set[str]:
    """Extract the set of tickers eligible on ``day`` from a kind's state.

    KIND_REGISTRY plugins use different state shapes:
      * connors_rsi2 → ``state.eligible_by_date[day]`` is a set[str]
      * xs_short_term_reversal → ``state.bottom_n_by_date[day]`` is a set[str]
      * clenow_momentum → ``state.ranks_by_date[day]`` is a set[str]
        (also requires ``state.benchmark_regime_ok[day]`` is True)
      * dual_ma_trend_following → per-ticker state via ``replay()``; the
        precompute returns None, so the caller iterates replay per ticker.
    """
    for attr in ("eligible_by_date", "bottom_n_by_date", "ranks_by_date"):
        m = getattr(state, attr, None)
        if isinstance(m, dict):
            return set(m.get(day, set()))
    return set()


def _compute_atr(df: pd.DataFrame, period: int) -> Optional[float]:
    if len(df) < period + 1:
        return None
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    close = df["Close"].to_numpy(dtype=float)
    prev_close = np.roll(close, 1)
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    tr[0] = high[0] - low[0]
    return float(pd.Series(tr).rolling(window=period, min_periods=period).mean().iloc[-1])


# ---------------------------------------------------------------- candidate composition


_SECTOR_ETF_HEURISTIC = {
    # Tech-heavy mega caps → XLK by default. This is a temporary stand-in
    # until a proper sector-classification table is wired (post-MVP). The
    # paper-auto sector cap (20%) uses this for grouping.
    "default": "XLK",
}


def _sector_etf_for(ticker: str) -> str:
    """Best-effort sector-ETF assignment. Defaults to XLK.

    Eventual replacement: a per-ticker sector lookup table at
    ``tools/data/ticker_sector.yml`` populated from Yahoo/SEC mappings.
    """
    return _SECTOR_ETF_HEURISTIC.get(ticker, _SECTOR_ETF_HEURISTIC["default"])


def _candidate_from_signal(
    ticker: str,
    df: pd.DataFrame,
    *,
    setup: str,
    kind: str,
    params: dict[str, Any],
    limit_offset_pct: float,
    account_net_liq: float,
    regime_class: str,
    cash_available: Optional[float],
    n_eligible_total: Optional[int] = None,
    signal_date: Optional[Any] = None,
    track: Optional[str] = None,
) -> Optional[CandidateInput]:
    """Compose a sized :class:`CandidateInput` for ``ticker``.

    Uses the LAST close in ``df`` as the pivot. Limit = pivot × (1 + offset).
    Stop = pivot − ATR × ``atr_stop_multiple``. No fixed target.
    Shares are computed by :func:`tools.position_sizer.compute` using
    ``account_net_liq`` + ``regime_class`` + the strategy's grade ("B").
    Returns None when the sizer's binding constraint zeros the share count.
    """
    if len(df) == 0:
        return None
    pivot = float(df["Close"].iloc[-1])
    if pivot <= 0:
        return None

    atr_period = int(params.get("atr_period", 20))
    atr_mult = float(params.get("atr_stop_multiple", 2.0))
    atr = _compute_atr(df, period=atr_period)
    if atr is None or atr <= 0:
        return None

    stop_price = pivot - atr_mult * atr
    if stop_price >= pivot:
        return None
    # Entry pricing split by setup class (fix 2026-06-16): momentum fills at the
    # open via a marketable limit (matches the next-open backtest fill);
    # reversion rests at the prior close. Size on this same price so the 5%
    # concentration cap holds at the worst-case fill. (limit_offset_pct retained
    # in the signature for back-compat; superseded by entry_pricing.)
    limit_price = entry_pricing.entry_limit_price(kind, pivot)

    try:
        sizer = position_sizer.compute(
            account=account_net_liq,
            entry_price=limit_price,
            atr=atr,
            setup_grade="B",
            regime_class=regime_class,
            atr_multiple=atr_mult,
            cash_available=cash_available,
            # 2026-05-28 fix: clamp to CLAUDE.md's 5% per-position hard rule.
            # position_sizer's default concentration_cap_pct=0.25 (25%) is
            # the doctrine's "relaxed" sizing limit, but the paper-auto track
            # is governed by the same 5% cap as the human track via
            # tools.auto_paper.pipeline._check_track_limits. Without this
            # clamp, ATR-tight-stop quant picks size to 7-11% of net liq and
            # get unconditionally rejected at the pipeline cap — silent zero-
            # placement outcome despite valid signals. Pass 0.05 here so the
            # binding constraint is the min(risk_budget, 5%_cap) inside the
            # sizer, never the pipeline's after-the-fact rejection.
            concentration_cap_pct=0.05,
        )
    except ValueError:
        return None
    shares = int(sizer.output.get("shares", 0))
    if shares <= 0:
        return None

    # Phase 3 plumbing — capture eligibility evidence so the
    # `swing-critic-quant-insight` critic has rank-within-rebalance
    # context. Light-touch v1: the kind-state structures only preserve
    # the *set* of eligible tickers (not the sorted ranking), so we
    # capture n_eligible_total (sparseness indicator) and leave
    # signal_rank + signal_percentile as None. Future enhancement:
    # extend each kind's State dataclass with sorted scores so this can
    # become a real rank.
    reasoning_trace: list[dict[str, Any]] = []
    if n_eligible_total is not None:
        trace_entry = TraceEntry(
            tool="tools/auto_paper/quant_scanner.py:eligibility_evidence",
            inputs={
                "ticker": ticker.upper(),
                "setup": setup,
                "signal_date": (
                    signal_date.isoformat() if hasattr(signal_date, "isoformat")
                    else str(signal_date) if signal_date is not None else None
                ),
            },
            output={
                "n_eligible_total": n_eligible_total,
                "ticker_in_eligible": True,
                "signal_rank": None,        # not preserved by kind-state shapes (v1 limitation)
                "signal_percentile": None,  # ditto
                "sparseness_flag": (
                    "sparse" if n_eligible_total <= 2
                    else "moderate" if n_eligible_total <= 5
                    else "dense"
                ),
                "source": f"tools.quant_strategies._kinds.{setup}.precompute state",
            },
        )
        reasoning_trace.append(trace_entry.to_dict())

    # Round to US-equity tick size ($0.01). Tiger rejects orders with
    # sub-cent precision (code=1200 "Sorry, your order price does not
    # match the tick size: 0.01"). Caught during 2026-05-26 manual
    # rebalance when 4-decimal limits all bounced. Stop price gets
    # placed as a broker STP SELL at reconciliation — same tick
    # constraint applies.
    return CandidateInput(
        ticker=ticker.upper(),
        setup_type=setup,
        setup_grade="B",
        pivot_price=pivot,
        limit_price=round(limit_price, 2),
        stop_price=round(stop_price, 2),
        target_price=None,
        shares=shares,
        sector_etf=_sector_etf_for(ticker),
        reasoning_trace=reasoning_trace,
        track=track,
    )


# ---------------------------------------------------------------- orchestrator


def scan_setup(
    row: dict[str, Any],
    *,
    account_net_liq: float,
    regime_class: str = "stage_2_confirmed",
    cash_available: Optional[float] = None,
    today: Optional[date] = None,
    force_refetch: bool = False,
    limit_offset_pct: float = 0.002,
    universe_dfs: Optional[dict[str, pd.DataFrame]] = None,
) -> ScannerReport:
    """Scan ONE deployable-setups row for live signals.

    Args:
        row: A single entry from ``deployable_setups.yml``'s ``deployable:``
            list. Must have ``setup`` (kind name in KIND_REGISTRY).
        account_net_liq: Paper-auto account equity ($). Threaded to
            :func:`tools.position_sizer.compute` for share counts.
        regime_class: Current SPY regime per :mod:`tools.regime_check`.
            Drives the sizer's regime multiplier (1.0 / 0.75 / 0.5 / 0.0).
        cash_available: Optional cash sanity cap for the sizer.
        today: Override today's date (for testing). Default: ``date.today()``.
        force_refetch: Pass-through to :mod:`tools.backtest.data_cache`.
        limit_offset_pct: Limit-buy offset above pivot. Default 0.002 = 20bp,
            consistent with the swing framework's ``ask + 0.1-0.2%`` rule.
        universe_dfs: Optional pre-loaded OHLCV map. When provided the
            scanner skips data_cache fetches entirely (used by tests and
            by :func:`scan_today` to share one fetch across setups).

    Returns:
        :class:`ScannerReport` with the candidates + eligibility detail.
    """
    today = today or date.today()
    setup = row["setup"]

    # The row's `setup` field is a spec FILENAME (e.g.
    # ``clenow_momentum_liquid_us``). Multiple spec files can share the same
    # underlying strategy ``kind:`` (e.g. ``clenow_momentum.yml`` and
    # ``clenow_momentum_liquid_us.yml`` both declare ``kind: clenow_momentum``).
    # KIND_REGISTRY is keyed on the ``kind:`` value, NOT the spec filename —
    # so we must load the spec first and dereference via ``spec["kind"]``.
    try:
        spec_path = _find_spec_for(setup)
    except FileNotFoundError as exc:
        return ScannerReport(
            setup=setup, spec_path="", eligible_tickers=[],
            candidates=[], signal_date=None,
            note=str(exc),
        )
    spec = yaml.safe_load(spec_path.read_text())
    kind = spec.get("kind")
    if kind not in KIND_REGISTRY:
        return ScannerReport(
            setup=setup, spec_path=str(spec_path), eligible_tickers=[],
            candidates=[], signal_date=None,
            note=f"spec kind {kind!r} not in KIND_REGISTRY",
        )
    params = _live_params_for(row, spec)
    benchmark = spec["universe"]["benchmark"]

    # Resolves either spec["universe"]["name"] (registered) or inline tickers.
    tickers = resolve_universe_tickers(spec)
    if benchmark not in tickers:
        tickers.append(benchmark)

    if universe_dfs is None:
        # Bug-2 fix: anchor live history at spec.period.start so the
        # rebalance schedule each kind's precompute() computes matches the
        # BACKTEST's rebalance schedule. Without this, range(start_idx, n, step)
        # over a moving 400-day window slides daily and the schedule never
        # aligns with the backtest's anchor — scanner queries eligibility at
        # signal_date which is rarely a real rebalance day.
        spec_start = spec.get("period", {}).get("start")
        fetch_start_date = (
            date.fromisoformat(spec_start) if isinstance(spec_start, str) else None
        )
        universe_dfs = _refresh_universe(
            tickers,
            start_date=fetch_start_date,
            force_refetch=force_refetch,
        )
    if benchmark not in universe_dfs:
        return ScannerReport(
            setup=setup, spec_path=str(spec_path), eligible_tickers=[],
            candidates=[], signal_date=None,
            note=f"benchmark {benchmark!r} failed to load",
        )

    kind_mod = KIND_REGISTRY[kind]
    state = kind_mod.precompute(universe_dfs, params)

    signal_date = _last_trading_date(universe_dfs[benchmark])
    if signal_date is None:
        return ScannerReport(
            setup=setup, spec_path=str(spec_path), eligible_tickers=[],
            candidates=[], signal_date=None,
            note="no benchmark bars available",
        )

    eligible = _eligible_tickers_on(state, signal_date)
    eligible -= {benchmark}
    n_eligible_total = len(eligible)

    # Strategy-discovery track (Alfred Delta 6) sourced from the deployable
    # row. None when absent — back-compat with pre-2026-05-29 rows.
    row_track = row.get("track")
    candidates: list[CandidateInput] = []
    for t in sorted(eligible):
        if t not in universe_dfs:
            continue
        cand = _candidate_from_signal(
            t, universe_dfs[t],
            setup=setup, kind=kind, params=params,
            limit_offset_pct=limit_offset_pct,
            account_net_liq=account_net_liq,
            regime_class=regime_class,
            cash_available=cash_available,
            n_eligible_total=n_eligible_total,
            signal_date=signal_date,
            track=row_track,
        )
        if cand is not None:
            candidates.append(cand)

    return ScannerReport(
        setup=setup,
        spec_path=str(spec_path),
        eligible_tickers=sorted(eligible),
        candidates=candidates,
        signal_date=signal_date.date() if isinstance(signal_date, pd.Timestamp) else signal_date,
    )


def scan_today(
    *,
    account_net_liq: float,
    regime_class: str = "stage_2_confirmed",
    cash_available: Optional[float] = None,
    today: Optional[date] = None,
    force_refetch: bool = False,
    deployable_path: Optional[str] = None,
) -> list[ScannerReport]:
    """Scan ALL deployable KIND_REGISTRY setups for live signals.

    Args:
        account_net_liq: Paper-auto account equity ($). Threaded into
            position-sizing for every candidate.
        regime_class: Current SPY regime (drives sizer's regime multiplier).
        cash_available: Optional cash sanity cap (per-position).
        today: Override today's date (for testing).
        force_refetch: Pass-through to :mod:`tools.backtest.data_cache`.
        deployable_path: Override path to ``deployable_setups.yml``.

    Returns:
        One :class:`ScannerReport` per deployable KIND_REGISTRY row.
        SETUP_REPLAY_REGISTRY rows are skipped (they have their own flow).
    """
    data = config.load(deployable_path)
    rows = data.get("deployable", []) or []
    reports: list[ScannerReport] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        setup = row.get("setup")
        if setup is None:
            continue
        # HOLD gate (2026-06-15): skip parked rows (``hold: true``). They
        # cleared the backtest gate but are NOT approved for live placement.
        # Mirrors config.deployable_setup_names() so parked strategies (e.g.
        # connors_rsi2, parked 2026-06-09) don't leak candidates into the v2
        # scan only to be deferred downstream by the critic panel.
        if row.get("hold", False):
            continue
        # Don't pre-filter by KIND_REGISTRY here — the row's `setup` is a
        # spec FILENAME, not a kind name. scan_setup loads the spec and
        # dereferences via ``spec["kind"]``; rows whose spec is missing or
        # whose kind isn't registered surface as a ScannerReport with a
        # non-empty ``note`` (SETUP_REPLAY_REGISTRY rows naturally land
        # here — they have no quant_strategies spec).
        report = scan_setup(
            row,
            account_net_liq=account_net_liq,
            regime_class=regime_class,
            cash_available=cash_available,
            today=today,
            force_refetch=force_refetch,
        )
        reports.append(report)
    return reports


# ---------------------------------------------------------------- CLI


def _format_report(report: ScannerReport) -> str:
    if not report.eligible_tickers:
        return (
            f"## {report.setup}\n"
            f"- Signal date: {report.signal_date}\n"
            f"- Eligible tickers: 0\n"
            f"- Note: {report.note or 'no eligibility on signal date'}\n"
        )
    lines = [
        f"## {report.setup}",
        f"- Signal date: {report.signal_date}",
        f"- Eligible tickers ({len(report.eligible_tickers)}): {', '.join(report.eligible_tickers)}",
        f"- Candidates ({len(report.candidates)}):",
    ]
    for c in report.candidates:
        lines.append(
            f"  - {c.ticker}: pivot=${c.pivot_price:.2f} "
            f"limit=${c.limit_price:.2f} stop=${c.stop_price:.2f}"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(prog="tools.auto_paper.quant_scanner")
    p.add_argument("--force-refetch", action="store_true",
                   help="Force re-fetch of universe OHLCV from yfinance")
    p.add_argument("--setup", default=None,
                   help="Scan only this setup (default: all deployable KIND_REGISTRY)")
    p.add_argument("--account", type=float, default=None,
                   help="Override account net_liq ($). Default: pull live from Tiger paper.")
    p.add_argument("--regime", default="stage_2_confirmed",
                   choices=["stage_2_confirmed", "stage_2_weakening",
                            "stage_3_transitional", "stage_4"],
                   help="Override regime classification. Default: stage_2_confirmed.")
    args = p.parse_args()

    account_net_liq = args.account
    cash_available = None
    if account_net_liq is None:
        from tools.broker.tiger import TigerClient
        client = TigerClient()
        summary = client.account_summary().output
        account_net_liq = float(summary["net_liquidation"])
        cash_available = float(summary.get("cash", 0)) or None

    reports = scan_today(
        account_net_liq=account_net_liq,
        regime_class=args.regime,
        cash_available=cash_available,
        force_refetch=args.force_refetch,
    )
    if args.setup is not None:
        reports = [r for r in reports if r.setup == args.setup]
    if not reports:
        print("# No deployable KIND_REGISTRY setups found.")
        return
    total_cands = sum(len(r.candidates) for r in reports)
    print(f"# Quant-scanner: {len(reports)} deployable setup(s), {total_cands} total candidate(s)\n")
    for r in reports:
        print(_format_report(r))


if __name__ == "__main__":
    main()
