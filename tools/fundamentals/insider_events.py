"""Insider buying-events file — the seam between EDGAR ingest and the backtest.

Composes Phases 2-4 (ingest → classify → conviction) over a universe + date
range into a flat list of qualifying buying *events*, persisted to a YAML file.
The :mod:`tools.quant_strategies._kinds.event_insider_buying` KIND loads that
file in ``precompute`` and replays it through the simulator. This separation is
deliberate: building events is slow + network-bound (parse ~2k Form 4s/day,
classify each insider's history); replaying them must be fast + deterministic.

ANTI-LOOK-AHEAD is inherited end-to-end: events are keyed off
``acceptanceDateTime`` (Phase 2), insider tiers are scored as-of the cluster
date with realized-window-only history (Phase 3), and the KIND enters on the
first bar strictly after the event date (Phase 5). No future information leaks
into an event's existence, its conviction, or its entry.

``build_events`` is dependency-injected (``ingest_day_fn`` / ``classify_fn`` /
``shares_outstanding_fn``) so the composition is testable without network.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .insider_conviction import cluster_purchases, compute_conviction

CONVICTION_RANK = {"exclude": 0, "low": 1, "medium": 2, "high": 3}


@dataclass
class InsiderEvent:
    ticker: str
    event_date: str                  # acceptance calendar date (anti-look-ahead)
    conviction_level: str
    composite_score: float
    n_insiders: int
    best_tier: str
    total_value: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def write_events(path: Path, events: list[InsiderEvent], *, meta: dict[str, Any]) -> None:
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": "1.0",
        "built_at": _utc_now_iso(),
        **meta,
        "n_events": len(events),
        "events": [e.to_dict() for e in events],
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def load_events(
    path: Path,
    *,
    min_conviction: str = "medium",
    universe: Optional[set[str]] = None,
) -> dict[str, list[InsiderEvent]]:
    """Load + filter events, grouped by ticker.

    Filters to ``conviction_level >= min_conviction`` and (if given) tickers in
    ``universe``. Returns ``{TICKER: [InsiderEvent, ...]}`` sorted by date.
    """
    import yaml
    min_rank = CONVICTION_RANK.get(min_conviction, 2)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    rows = doc.get("events", []) if isinstance(doc, dict) else []
    out: dict[str, list[InsiderEvent]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        tkr = str(r.get("ticker", "")).upper()
        if not tkr or (universe is not None and tkr not in universe):
            continue
        if CONVICTION_RANK.get(str(r.get("conviction_level", "")), 0) < min_rank:
            continue
        try:
            ev = InsiderEvent(
                ticker=tkr,
                event_date=str(r["event_date"]),
                conviction_level=str(r["conviction_level"]),
                composite_score=float(r.get("composite_score", 0.0)),
                n_insiders=int(r.get("n_insiders", 0)),
                best_tier=str(r.get("best_tier", "unrated")),
                total_value=float(r.get("total_value", 0.0)),
            )
        except (KeyError, TypeError, ValueError):
            continue
        out.setdefault(tkr, []).append(ev)
    for tkr in out:
        out[tkr].sort(key=lambda e: e.event_date)
    return out


# ---------------------------------------------------------------------------
# build (composes Phases 2-4; dependency-injected)
# ---------------------------------------------------------------------------


def build_events(
    universe: list[str],
    start: date,
    end: date,
    *,
    ingest_day_fn: Callable[[str], list[Any]],
    classify_fn: Callable[[str, date], Any],
    shares_outstanding_fn: Callable[[str, date], Optional[float]],
    horizon_days: int = 126,
    min_conviction: str = "medium",
    cluster_window_days: int = 2,
    rnd_intensity_fn: Optional[Callable[[str, date], Optional[float]]] = None,
) -> list[InsiderEvent]:
    """Build qualifying insider buying events over ``universe`` × [start, end].

    Args (injected for testability + network isolation):
        ingest_day_fn: ``"YYYY-MM-DD" -> list[InsiderPurchase]`` (Phase 2).
        classify_fn: ``(insider_cik, asof_date) -> TrackRecordStats`` (Phase 3).
        shares_outstanding_fn: ``(ticker, asof_date) -> shares | None``.
        rnd_intensity_fn: optional ``(ticker, asof_date) -> R&D/rev | None``.
    """
    univ = {t.upper() for t in universe}
    by_ticker: dict[str, list[Any]] = {}
    d = start
    while d <= end:
        for p in ingest_day_fn(d.isoformat()):
            tkr = (getattr(p, "ticker", None) or "")
            tkr = str(tkr).upper() if tkr else ""
            if tkr in univ:
                by_ticker.setdefault(tkr, []).append(p)
        d += timedelta(days=1)

    min_rank = CONVICTION_RANK.get(min_conviction, 2)
    events: list[InsiderEvent] = []
    tier_cache: dict[tuple[str, str], str] = {}

    for tkr, purchases in by_ticker.items():
        for cluster in cluster_purchases(purchases, window_days=cluster_window_days):
            cdates = [
                date.fromisoformat(str(getattr(p, "event_date", ""))[:10])
                for p in cluster if getattr(p, "event_date", None)
            ]
            if not cdates:
                continue
            cdate = max(cdates)

            tiers: dict[str, str] = {}
            for p in cluster:
                cik = str(getattr(p, "insider_cik", "") or "")
                if not cik:
                    continue
                ck = (cik, cdate.isoformat())
                if ck not in tier_cache:
                    try:
                        tier_cache[ck] = classify_fn(cik, cdate).tier
                    except Exception:  # noqa: BLE001
                        tier_cache[ck] = "unrated"
                tiers[cik] = tier_cache[ck]

            try:
                so = shares_outstanding_fn(tkr, cdate)
            except Exception:  # noqa: BLE001
                so = None
            rnd = None
            if rnd_intensity_fn is not None:
                try:
                    rnd = rnd_intensity_fn(tkr, cdate)
                except Exception:  # noqa: BLE001
                    rnd = None

            score = compute_conviction(
                tkr, cluster, shares_outstanding=so,
                insider_tiers=tiers, rnd_intensity=rnd,
            )
            if CONVICTION_RANK.get(score.level, 0) < min_rank:
                continue
            events.append(InsiderEvent(
                ticker=tkr,
                event_date=score.event_date or cdate.isoformat(),
                conviction_level=score.level,
                composite_score=score.composite_score,
                n_insiders=score.n_distinct_insiders,
                best_tier=score.best_tier,
                total_value=score.total_value,
            ))

    events.sort(key=lambda e: (e.event_date, e.ticker))
    return events
