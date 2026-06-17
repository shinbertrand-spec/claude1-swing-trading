"""Opportunistic-insider classifier — the alpha core of the insider KIND.

Not every insider purchase is signal. The predictive ones come from insiders
with a *track record* of well-timed, profitable open-market buys (Cohen, Malloy
& Pomorski 2012, "Decoding Inside Information" — opportunistic insiders earn
meaningful forward abnormal returns; routine buyers do not). This module scores
an insider by the forward **abnormal** return (vs a benchmark) of their PRIOR
purchases and assigns a profitability tier.

ANTI-LOOK-AHEAD (load-bearing). When scoring an insider AT an event date, a
prior buy may only count if its full forward window has *already closed* before
that event date. You cannot "know" a 6-month outcome that hasn't happened yet.
:func:`compute_track_record` enforces ``forward_window_exit_date <= asof`` on
every prior buy — independent of how much history the price source returns — so
the score is identical in live scoring and in backtest replay.

The tier thresholds are documented, **un-tuned priors** (see :data:`TIER_RULES`).
They are NOT fitted to make any strategy pass — Phase 6's net-of-cost gate is
the arbiter. Do not tune them to force a pass.

Scoring is pure and price-injected (``close_series_fn``), so it is fully
testable with synthetic series. EDGAR sourcing of an insider's own history is a
separate best-effort helper.

CLI::

    uv run python -m tools.fundamentals.insider_track_record --cik 0001977231 --asof 2026-06-15
"""
from __future__ import annotations

import argparse
import bisect
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Callable, Optional

from ..cli import emit
from ..contract import TraceEntry

TOOL = "tools/fundamentals/insider_track_record.py"

# ~6 months of trading days — matches the KIND's 6-month fixed hold (Phase 5).
DEFAULT_HORIZON_DAYS = 126
DEFAULT_MIN_HISTORY = 3          # need >=3 realized prior buys to rate an insider
DEFAULT_BENCHMARK = "SPY"

CloseSeriesFn = Callable[[str], Optional["DateClose"]]


class InsiderTier(str, Enum):
    ELITE = "elite"
    GOOD = "good"
    NEUTRAL = "neutral"
    POOR = "poor"
    UNRATED = "unrated"          # insufficient realized history


# Un-tuned priors. Evaluated top-down; first match wins (after UNRATED gate).
# (tier, min_mean_abnormal, min_hit_rate). POOR is the negative fallthrough.
TIER_RULES = (
    (InsiderTier.ELITE, 0.10, 0.60),
    (InsiderTier.GOOD, 0.03, 0.50),
)


# ---------------------------------------------------------------------------
# date-indexed close series (lightweight, dependency-free)
# ---------------------------------------------------------------------------


@dataclass
class DateClose:
    """A sorted, date-indexed close series. ``dates`` strictly ascending."""

    dates: list[date]
    closes: list[float]

    def __len__(self) -> int:
        return len(self.dates)

    def next_bar_index_after(self, d: date) -> Optional[int]:
        """Index of the first bar with date strictly greater than ``d``."""
        i = bisect.bisect_right(self.dates, d)
        return i if i < len(self.dates) else None

    def asof(self, d: date) -> Optional[float]:
        """Close on ``d`` or the most recent prior bar (<= d)."""
        i = bisect.bisect_right(self.dates, d) - 1
        return self.closes[i] if i >= 0 else None


# ---------------------------------------------------------------------------
# forward abnormal return
# ---------------------------------------------------------------------------


@dataclass
class ForwardReturn:
    ticker: str
    event_date: str
    entry_date: str
    exit_date: str
    horizon_days: int
    stock_return: float
    benchmark_return: float
    abnormal_return: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def forward_abnormal_return(
    ticker: str,
    event_date: date,
    *,
    horizon_days: int,
    close_series_fn: CloseSeriesFn,
    benchmark: str = DEFAULT_BENCHMARK,
) -> Optional[ForwardReturn]:
    """Forward abnormal return of a buy: stock fwd return minus benchmark's.

    Entry = first trading bar STRICTLY AFTER ``event_date`` (next-bar convention,
    no look-ahead into the event bar). Exit = ``horizon_days`` bars later. The
    benchmark return is measured over the same calendar window via ``asof``.

    Returns ``None`` if either series lacks a fully-realized window.
    """
    s = close_series_fn(ticker)
    if s is None or len(s) == 0:
        return None
    entry_i = s.next_bar_index_after(event_date)
    if entry_i is None:
        return None
    exit_i = entry_i + horizon_days
    if exit_i >= len(s):
        return None

    entry_px, exit_px = s.closes[entry_i], s.closes[exit_i]
    if entry_px <= 0:
        return None
    stock_ret = exit_px / entry_px - 1.0
    entry_date, exit_date = s.dates[entry_i], s.dates[exit_i]

    bench_ret = 0.0
    b = close_series_fn(benchmark)
    if b is not None and len(b) > 0:
        b_entry = b.asof(entry_date)
        b_exit = b.asof(exit_date)
        if b_entry and b_exit and b_entry > 0:
            bench_ret = b_exit / b_entry - 1.0

    return ForwardReturn(
        ticker=ticker,
        event_date=event_date.isoformat(),
        entry_date=entry_date.isoformat(),
        exit_date=exit_date.isoformat(),
        horizon_days=horizon_days,
        stock_return=round(stock_ret, 6),
        benchmark_return=round(bench_ret, 6),
        abnormal_return=round(stock_ret - bench_ret, 6),
    )


# ---------------------------------------------------------------------------
# track record + tier
# ---------------------------------------------------------------------------


@dataclass
class TrackRecordStats:
    insider_cik: str
    insider_name: str
    asof: str
    horizon_days: int
    benchmark: str
    n_prior_buys: int            # prior buys with event_date < asof
    n_realized: int              # of those, with forward window closed before asof
    n_scored: int                # of realized, with a computable abnormal return
    hit_rate: Optional[float]    # fraction of scored buys with abnormal > 0
    mean_abnormal: Optional[float]
    median_abnormal: Optional[float]
    tier: str
    rationale: str
    samples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def profitability_tier(
    n_scored: int,
    hit_rate: Optional[float],
    mean_abnormal: Optional[float],
    *,
    min_history: int = DEFAULT_MIN_HISTORY,
) -> tuple[InsiderTier, str]:
    """Map track-record stats to a tier. Documented un-tuned priors."""
    if n_scored < min_history or hit_rate is None or mean_abnormal is None:
        return InsiderTier.UNRATED, (
            f"insufficient realized history (n={n_scored} < {min_history})"
        )
    for tier, min_mean, min_hit in TIER_RULES:
        if mean_abnormal >= min_mean and hit_rate >= min_hit:
            return tier, (
                f"mean abnormal {mean_abnormal:.1%}, hit-rate {hit_rate:.0%} "
                f"over {n_scored} realized buys"
            )
    if mean_abnormal < 0.0 or hit_rate < 0.40:
        return InsiderTier.POOR, (
            f"mean abnormal {mean_abnormal:.1%}, hit-rate {hit_rate:.0%} — "
            f"no edge over {n_scored} realized buys"
        )
    return InsiderTier.NEUTRAL, (
        f"mean abnormal {mean_abnormal:.1%}, hit-rate {hit_rate:.0%} — "
        f"middling over {n_scored} realized buys"
    )


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def compute_track_record(
    prior_purchases: list[Any],
    asof: date,
    *,
    close_series_fn: CloseSeriesFn,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    min_history: int = DEFAULT_MIN_HISTORY,
    benchmark: str = DEFAULT_BENCHMARK,
    insider_cik: str = "",
    insider_name: str = "",
) -> TrackRecordStats:
    """Score an insider's prior purchases as of ``asof`` (anti-look-ahead).

    ``prior_purchases`` are :class:`InsiderPurchase`-like objects (need
    ``ticker`` + ``event_date`` ISO string). Only buys whose forward window has
    *closed* on or before ``asof`` contribute — a buy made yesterday tells us
    nothing yet and is excluded.
    """
    abnormals: list[float] = []
    samples: list[dict[str, Any]] = []
    n_prior = 0
    n_realized = 0

    for p in prior_purchases:
        ev = _event_date_of(p)
        if ev is None or ev >= asof:
            continue                      # not strictly prior to the scoring date
        n_prior += 1
        ticker = getattr(p, "ticker", None) or (p.get("ticker") if isinstance(p, dict) else None)
        if not ticker:
            continue
        fr = forward_abnormal_return(
            str(ticker), ev, horizon_days=horizon_days,
            close_series_fn=close_series_fn, benchmark=benchmark,
        )
        if fr is None:
            continue
        # ANTI-LOOK-AHEAD: the forward window must have closed on/before asof.
        if date.fromisoformat(fr.exit_date) > asof:
            continue
        n_realized += 1
        abnormals.append(fr.abnormal_return)
        samples.append(fr.to_dict())

    n_scored = len(abnormals)
    hit_rate = (sum(1 for a in abnormals if a > 0) / n_scored) if n_scored else None
    mean_abn = (sum(abnormals) / n_scored) if n_scored else None
    med_abn = _median(abnormals) if n_scored else None

    tier, rationale = profitability_tier(
        n_scored, hit_rate, mean_abn, min_history=min_history)

    return TrackRecordStats(
        insider_cik=insider_cik,
        insider_name=insider_name,
        asof=asof.isoformat(),
        horizon_days=horizon_days,
        benchmark=benchmark,
        n_prior_buys=n_prior,
        n_realized=n_realized,
        n_scored=n_scored,
        hit_rate=round(hit_rate, 4) if hit_rate is not None else None,
        mean_abnormal=round(mean_abn, 6) if mean_abn is not None else None,
        median_abnormal=round(med_abn, 6) if med_abn is not None else None,
        tier=tier.value,
        rationale=rationale,
        samples=samples,
    )


def _event_date_of(p: Any) -> Optional[date]:
    raw = getattr(p, "event_date", None)
    if raw is None and isinstance(p, dict):
        raw = p.get("event_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# price source (default, cached) + EDGAR sourcing (best-effort)
# ---------------------------------------------------------------------------


_SERIES_CACHE: dict[str, Optional[DateClose]] = {}


def default_close_series_fn(start: Optional[str] = None) -> CloseSeriesFn:
    """Build a close-series fn backed by the backtest parquet cache (yfinance).

    Memoised per process. ``start`` bounds the fetch window (defaults to the
    cache's 5y default). Returns None for tickers with no data.
    """
    def _fn(ticker: str) -> Optional[DateClose]:
        key = ticker.upper()
        if key in _SERIES_CACHE:
            return _SERIES_CACHE[key]
        try:
            from ..backtest import data_cache
            try:
                df = data_cache.load(key)
            except FileNotFoundError:
                data_cache.fetch(key, start=start)
                df = data_cache.load(key)
            closes = df["Close"]
            dates = [ix.date() if hasattr(ix, "date") else ix for ix in closes.index]
            dc = DateClose(dates=list(dates), closes=[float(c) for c in closes.tolist()])
        except Exception:  # noqa: BLE001
            dc = None
        _SERIES_CACHE[key] = dc
        return dc
    return _fn


def fetch_insider_history(
    insider_cik: str,
    *,
    _filings_factory: Optional[Callable[[str], Any]] = None,
) -> list[Any]:
    """Best-effort: pull an insider's own Form 4 purchases via EDGAR (by CIK).

    Reuses Phase 2's :func:`parse_filing`. Returns a (possibly empty) list of
    :class:`InsiderPurchase`. Network-bound; never raises.
    """
    from .form4_insider_transactions import parse_filing

    try:
        if _filings_factory is not None:
            filings = _filings_factory(insider_cik)
        else:
            from edgar import get_entity, set_identity
            import os
            set_identity(os.environ.get("EDGAR_IDENTITY") or "Bertrand Shin shinbertrand@gmail.com")
            ent = get_entity(insider_cik)
            filings = ent.get_filings(form="4")
    except Exception:  # noqa: BLE001
        return []

    out: list[Any] = []
    for f in (filings or []):
        try:
            out.extend(parse_filing(f))
        except Exception:  # noqa: BLE001
            continue
    # keep only this insider's own rows (a Form 4 can name co-owners)
    return [p for p in out if str(getattr(p, "insider_cik", "")).lstrip("0") == str(insider_cik).lstrip("0")] or out


def classify_insider(
    insider_cik: str,
    asof: date,
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    min_history: int = DEFAULT_MIN_HISTORY,
    benchmark: str = DEFAULT_BENCHMARK,
    close_series_fn: Optional[CloseSeriesFn] = None,
    _filings_factory: Optional[Callable[[str], Any]] = None,
) -> TrackRecordStats:
    """Source an insider's history + score it. Composes the two layers."""
    history = fetch_insider_history(insider_cik, _filings_factory=_filings_factory)
    name = ""
    for p in history:
        nm = getattr(p, "insider_name", "") or ""
        if nm:
            name = nm
            break
    fn = close_series_fn or default_close_series_fn()
    return compute_track_record(
        history, asof, close_series_fn=fn, horizon_days=horizon_days,
        min_history=min_history, benchmark=benchmark,
        insider_cik=insider_cik, insider_name=name,
    )


def compute(insider_cik: str, asof: date, **kw) -> TraceEntry:
    stats = classify_insider(insider_cik, asof, **kw)
    return TraceEntry(
        tool=TOOL,
        inputs={"insider_cik": insider_cik, "asof": asof.isoformat()},
        output=stats.to_dict(),
    )


def main() -> None:
    p = argparse.ArgumentParser(prog="tools.fundamentals.insider_track_record")
    p.add_argument("--cik", required=True)
    p.add_argument("--asof", default=None, help="ISO date; default today")
    p.add_argument("--horizon", type=int, default=DEFAULT_HORIZON_DAYS)
    p.add_argument("--min-history", type=int, default=DEFAULT_MIN_HISTORY)
    args = p.parse_args()
    asof = date.fromisoformat(args.asof) if args.asof else datetime.now().date()
    emit(compute(args.cik, asof, horizon_days=args.horizon, min_history=args.min_history))


if __name__ == "__main__":
    main()
