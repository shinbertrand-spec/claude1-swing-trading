"""Baseline-honesty benchmarks — no performance number reported alone (A2).

Per CLAUDE.md § Evaluation-honesty policies: every paper/eval report shows,
on the IDENTICAL window, dumb baselines next to the strategy's numbers —
buy-and-hold and a dumb momentum rule — plus Sortino and max drawdown. The
baselines are computed here IN CODE (deterministic, testable), not composed
by the agent at render time.

Baselines (v1):

* ``spy_buy_hold`` — buy-and-hold SPY over the window (market baseline; SPY
  is the same benchmark the quant-strategist backtest reports use).
* ``equal_weight_traded_buy_hold`` — equal-weight buy-and-hold of the tickers
  the sleeve actually traded (answers "did the trading add anything over just
  holding the same names?").
* ``spy_ts_momentum_12_1`` — dumb 12-1 time-series momentum on SPY: long when
  trailing 12-month-minus-1-month return is positive, else cash. The simplest
  member of the family the platform's surviving setup (ts_momentum) belongs to.

All stats are daily-return based: Sharpe/Sortino annualised by sqrt(252),
max drawdown from the cumulative curve. Price data is injected via
``price_loader`` (offline in tests; ``tools.backtest.data_cache`` live).
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

import pandas as pd

TOOL = "tools/auto_paper/benchmarks.py"

TRADING_DAYS_PER_YEAR = 252
# 12-1 momentum lookback in trading days (12 months, skipping the last one).
_MOM_LONG = 252
_MOM_SKIP = 21

# price_loader(ticker, start, end) -> pd.Series of closes indexed by date-like.
PriceLoader = Callable[[str, _dt.date, _dt.date], pd.Series]


@dataclass
class BaselineStats:
    name: str
    total_return_pct: Optional[float]
    sharpe_annualised: Optional[float]
    sortino_annualised: Optional[float]
    max_drawdown_pct: Optional[float]
    n_days: int
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _stats_from_curve(name: str, curve: pd.Series, note: str = "") -> BaselineStats:
    """Compute the A2 stat set from an equity curve (starting value ~1.0)."""
    curve = curve.dropna()
    if len(curve) < 2:
        return BaselineStats(name, None, None, None, None, len(curve),
                             note or "insufficient data")
    rets = curve.pct_change().dropna()
    total_return_pct = round((float(curve.iloc[-1]) / float(curve.iloc[0]) - 1.0) * 100, 2)

    mean = float(rets.mean())
    std = float(rets.std(ddof=1))
    sharpe = round(mean / std * math.sqrt(TRADING_DAYS_PER_YEAR), 2) if std > 0 else None

    downside = rets[rets < 0]
    dd_std = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    sortino = round(mean / dd_std * math.sqrt(TRADING_DAYS_PER_YEAR), 2) if dd_std > 0 else None

    running_max = curve.cummax()
    drawdown = curve / running_max - 1.0
    max_dd_pct = round(float(drawdown.min()) * 100, 2)

    return BaselineStats(name, total_return_pct, sharpe, sortino, max_dd_pct,
                         len(curve), note)


def buy_and_hold(name: str, prices: pd.Series, note: str = "") -> BaselineStats:
    """Buy-and-hold stats over the given price series."""
    prices = prices.dropna()
    if len(prices) < 2:
        return BaselineStats(name, None, None, None, None, len(prices),
                             note or "insufficient data")
    return _stats_from_curve(name, prices / float(prices.iloc[0]), note)


def equal_weight_buy_and_hold(
    name: str, by_ticker: dict[str, pd.Series], note: str = ""
) -> BaselineStats:
    """Equal-weight at window start, no rebalancing, common dates only."""
    normalised = []
    for series in by_ticker.values():
        series = series.dropna()
        if len(series) >= 2:
            normalised.append(series / float(series.iloc[0]))
    if not normalised:
        return BaselineStats(name, None, None, None, None, 0,
                             note or "insufficient data")
    frame = pd.concat(normalised, axis=1, join="inner")
    if len(frame) < 2:
        return BaselineStats(name, None, None, None, None, len(frame),
                             note or "insufficient overlapping data")
    return _stats_from_curve(name, frame.mean(axis=1), note)


def ts_momentum_12_1(
    name: str,
    prices: pd.Series,
    note: str = "",
    window_start: Optional[_dt.date] = None,
) -> BaselineStats:
    """Dumb 12-1 time-series momentum: long next day iff the return from
    t-252 to t-21 trading days is positive; else cash (0%). ``prices`` must
    include ~13 months of history BEFORE the scored window. Stats cover the
    bars where the signal is defined; ``window_start`` additionally clips the
    scored curve to the report window so the baseline is measured on the
    IDENTICAL window as the sleeve (A2), not the whole lookback span."""
    prices = prices.dropna()
    if len(prices) <= _MOM_LONG + 1:
        return BaselineStats(name, None, None, None, None, 0,
                             note or f"needs > {_MOM_LONG + 1} bars incl. lookback")
    signal = (prices.shift(_MOM_SKIP) / prices.shift(_MOM_LONG) - 1.0) > 0
    rets = prices.pct_change()
    strat_rets = rets.where(signal.shift(1).fillna(False), 0.0).iloc[_MOM_LONG + 1:]
    if window_start is not None:
        strat_rets = strat_rets[[ts.date() >= window_start for ts in strat_rets.index]]
    if len(strat_rets) < 1:
        return BaselineStats(name, None, None, None, None, 0,
                             note or "signal lookback not covered before window")
    curve = (1.0 + strat_rets).cumprod()
    return _stats_from_curve(name, curve, note)


def _default_price_loader(ticker: str, start: _dt.date, end: _dt.date) -> pd.Series:
    """Live loader over the OHLCV cache (A3: end-bounded at the adapter)."""
    from tools.backtest import data_cache

    data_cache.fetch(ticker, start=start, end=end + _dt.timedelta(days=1))
    df = data_cache.load(ticker, end=end)
    closes = df["Close"]
    return closes[[ts.date() >= start for ts in closes.index]]


def compute_baselines(
    *,
    window_start: _dt.date,
    window_end: _dt.date,
    traded_tickers: list[str],
    price_loader: Optional[PriceLoader] = None,
) -> dict[str, Any]:
    """Compute the A2 baseline set for [window_start, window_end].

    Never raises for a single failing leg — each baseline degrades to an
    ``insufficient data`` / error note so the report always renders.
    """
    loader = price_loader or _default_price_loader
    out: dict[str, Any] = {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "baselines": [],
    }
    lookback_start = window_start - _dt.timedelta(days=550)

    def _safe(fn: Callable[[], BaselineStats], name: str) -> BaselineStats:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — degrade, never crash the report
            return BaselineStats(name, None, None, None, None, 0, f"error: {exc}")

    spy_window: Optional[pd.Series] = None

    def _spy_bh() -> BaselineStats:
        nonlocal spy_window
        spy_window = loader("SPY", window_start, window_end)
        return buy_and_hold("spy_buy_hold", spy_window)

    out["baselines"].append(_safe(_spy_bh, "spy_buy_hold").to_dict())

    def _ew() -> BaselineStats:
        series = {t: loader(t, window_start, window_end) for t in sorted(set(traded_tickers))}
        return equal_weight_buy_and_hold(
            "equal_weight_traded_buy_hold", series,
            note=f"{len(series)} traded tickers, equal weight at window start",
        )

    out["baselines"].append(_safe(_ew, "equal_weight_traded_buy_hold").to_dict())

    def _mom() -> BaselineStats:
        full = loader("SPY", lookback_start, window_end)
        return ts_momentum_12_1(
            "spy_ts_momentum_12_1", full,
            note="long SPY iff trailing 12-1 return > 0, else cash",
            window_start=window_start,
        )

    out["baselines"].append(_safe(_mom, "spy_ts_momentum_12_1").to_dict())
    return out


def render_baselines_markdown(baselines: Optional[dict[str, Any]]) -> str:
    """Ready-to-inline markdown table for the report (A2: rendered by code,
    not composed in a prompt). Empty string when no baselines computed."""
    if not baselines or not baselines.get("baselines"):
        return ""
    lines = [
        f"Window: {baselines['window_start']} -> {baselines['window_end']} "
        f"(identical to realized-trade window)",
        "",
        "| Baseline | Total return | Sharpe | Sortino | Max DD | n days | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    for b in baselines["baselines"]:
        def _fmt(v: Optional[float], suffix: str = "") -> str:
            return f"{v}{suffix}" if v is not None else "-"
        lines.append(
            f"| {b['name']} | {_fmt(b['total_return_pct'], '%')} "
            f"| {_fmt(b['sharpe_annualised'])} | {_fmt(b['sortino_annualised'])} "
            f"| {_fmt(b['max_drawdown_pct'], '%')} | {b['n_days']} "
            f"| {b.get('note') or ''} |"
        )
    return "\n".join(lines)
