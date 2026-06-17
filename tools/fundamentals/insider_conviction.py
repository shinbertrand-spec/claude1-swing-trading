"""Conviction composite — fold insider track-record + event features into one score.

Phase 3 tells us whether an insider is worth listening to. Phase 4 combines that
with the *character* of a buying event — how many insiders, how big relative to
the company, what roles, how R&D-intensive the firm — into a single conviction
verdict the KIND can rank/gate on.

Encoded priors (documented, UN-TUNED — Phase 6's net-of-cost gate is the arbiter;
do not tune to force a pass):

  * Cluster: >=2 distinct insiders buying within <=2 days is a stronger signal
    than a lone buyer (corroboration).
  * Size as % shares outstanding: a buy under ~0.004% of shares is negligible
    (noise / token); >=~0.028% is a high-conviction commitment.
  * Role: a pure 10%-owner buyer is down-weighted to the point of exclusion when
    it is the *only* buyer — these are frequently passive holders / structured
    products (e.g. a bank reporting 1-share fractional lots), not signal. A CFO
    buy gets a mild tilt up (CFOs see the numbers first).
  * Track-record tier (Phase 3): elite/good amplify; poor strongly de-weights;
    unrated mildly de-weights.
  * High-R&D firms: insider buys are more informative where value is intangible
    and hard for outsiders to read (optional; applied only when intensity known).

Deterministic + pure: no network, no price data. Inputs are
:class:`InsiderPurchase`-like events + shares outstanding + a {cik->tier} map +
optional R&D intensity.

CLI is intentionally omitted — this composes inside the Phase 5 KIND, not as a
standalone lookup.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Optional

# --- cluster ---
CLUSTER_MIN_INSIDERS = 2
CLUSTER_WINDOW_DAYS = 2

# --- size as fraction of shares outstanding ---
SIZE_NEGLIGIBLE_PCT = 0.00004   # 0.004%
SIZE_HIGH_PCT = 0.00028         # 0.028%

# --- R&D intensity (R&D / revenue) above which buys are amplified ---
RND_HIGH_INTENSITY = 0.15

# --- multipliers (applied to a base of 1.0) ---
M_CLUSTER = 1.5
M_SIZE_NEGLIGIBLE = 0.3
M_SIZE_HIGH = 1.6
M_CFO = 1.2
M_TIER = {"elite": 1.5, "good": 1.2, "neutral": 1.0, "poor": 0.4, "unrated": 0.8}
M_RND_HIGH = 1.2

# --- composite → level thresholds ---
LEVEL_HIGH_AT = 2.0
LEVEL_MEDIUM_AT = 1.0


class ConvictionLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    EXCLUDE = "exclude"     # no effective (non-pure-10%-owner) buyers


class SizeBucket(str, Enum):
    NEGLIGIBLE = "negligible"
    NORMAL = "normal"
    HIGH = "high"
    UNKNOWN = "unknown"     # shares outstanding not available


@dataclass
class ConvictionScore:
    ticker: str
    event_date: Optional[str]
    n_events: int
    n_effective_events: int
    n_distinct_insiders: int
    is_cluster: bool
    total_shares: float
    total_value: float
    shares_outstanding: Optional[float]
    pct_shares_outstanding: Optional[float]
    size_bucket: str
    has_cfo: bool
    best_tier: str
    rnd_intensity: Optional[float]
    rnd_amplified: bool
    multipliers: dict[str, float]
    composite_score: float
    level: str
    rationale: str
    excluded_ten_pct_only: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _get(p: Any, key: str, default: Any = None) -> Any:
    if isinstance(p, dict):
        return p.get(key, default)
    return getattr(p, key, default)


def _event_date(p: Any) -> Optional[date]:
    raw = _get(p, "event_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _is_effective_buyer(p: Any) -> bool:
    """A purchase counts unless it is a PURE 10%-owner (no officer/director hat).

    Pure 10%-owner filings are routinely passive holders / structured products
    (the 1-share fractional bank lots seen in raw Form 4 ingest) — noise."""
    ten = bool(_get(p, "is_ten_pct_owner", False))
    officer = bool(_get(p, "is_officer", False))
    director = bool(_get(p, "is_director", False))
    return not (ten and not officer and not director)


def _is_cfo(p: Any) -> bool:
    title = (str(_get(p, "officer_title", "") or "") + " " + str(_get(p, "position", "") or "")).lower()
    return "cfo" in title or "chief financial" in title


def cluster_purchases(purchases: list[Any], *, window_days: int = CLUSTER_WINDOW_DAYS) -> list[list[Any]]:
    """Group a ticker's purchases into clusters by event-date proximity.

    Greedy: sorted by event_date, each event joins the current cluster if it is
    within ``window_days`` of the cluster's *first* event; otherwise it opens a
    new cluster. Events without a parseable date are dropped.
    """
    dated = [(d, p) for p in purchases if (d := _event_date(p)) is not None]
    dated.sort(key=lambda t: t[0])
    clusters: list[list[Any]] = []
    anchor: Optional[date] = None
    for d, p in dated:
        if anchor is None or (d - anchor).days > window_days:
            clusters.append([p])
            anchor = d
        else:
            clusters[-1].append(p)
    return clusters


# ---------------------------------------------------------------------------
# composite
# ---------------------------------------------------------------------------


def compute_conviction(
    ticker: str,
    events: list[Any],
    *,
    shares_outstanding: Optional[float] = None,
    insider_tiers: Optional[dict[str, str]] = None,
    rnd_intensity: Optional[float] = None,
) -> ConvictionScore:
    """Composite conviction for one buying cluster (events already grouped).

    ``insider_tiers`` maps insider_cik -> Phase 3 tier string; the BEST tier
    among effective buyers drives the tier multiplier (one strong insider
    corroborated by others is the high-signal case).
    """
    insider_tiers = insider_tiers or {}
    effective = [p for p in events if _is_effective_buyer(p)]
    excluded = [str(_get(p, "insider_name", "") or _get(p, "insider_cik", ""))
                for p in events if not _is_effective_buyer(p)]

    ev_date = None
    dates = [d for p in events if (d := _event_date(p)) is not None]
    if dates:
        ev_date = max(dates).isoformat()

    total_shares = sum(float(_get(p, "shares", 0) or 0) for p in effective)
    total_value = sum(float(_get(p, "value", 0) or 0) for p in effective)
    distinct = {str(_get(p, "insider_cik", "")) for p in effective if _get(p, "insider_cik")}
    n_distinct = len(distinct)
    is_cluster = n_distinct >= CLUSTER_MIN_INSIDERS

    # --- size bucket ---
    pct = None
    if shares_outstanding and shares_outstanding > 0:
        pct = total_shares / shares_outstanding
        if pct < SIZE_NEGLIGIBLE_PCT:
            size_bucket = SizeBucket.NEGLIGIBLE
        elif pct >= SIZE_HIGH_PCT:
            size_bucket = SizeBucket.HIGH
        else:
            size_bucket = SizeBucket.NORMAL
    else:
        size_bucket = SizeBucket.UNKNOWN

    has_cfo = any(_is_cfo(p) for p in effective)

    # --- best tier among effective buyers ---
    tiers = [insider_tiers.get(str(_get(p, "insider_cik", "")), "unrated") for p in effective]
    best_tier = _best_tier(tiers) if tiers else "unrated"

    rnd_amplified = rnd_intensity is not None and rnd_intensity >= RND_HIGH_INTENSITY

    # --- exclusion gate: no effective buyers → noise ---
    if not effective:
        return ConvictionScore(
            ticker=ticker, event_date=ev_date, n_events=len(events),
            n_effective_events=0, n_distinct_insiders=0, is_cluster=False,
            total_shares=0.0, total_value=0.0, shares_outstanding=shares_outstanding,
            pct_shares_outstanding=None, size_bucket=SizeBucket.UNKNOWN.value,
            has_cfo=False, best_tier="unrated", rnd_intensity=rnd_intensity,
            rnd_amplified=False, multipliers={}, composite_score=0.0,
            level=ConvictionLevel.EXCLUDE.value,
            rationale="no effective buyers — only pure 10%-owner filing(s); excluded as noise",
            excluded_ten_pct_only=excluded,
        )

    # --- multipliers ---
    mult: dict[str, float] = {}
    mult["cluster"] = M_CLUSTER if is_cluster else 1.0
    mult["size"] = {
        SizeBucket.NEGLIGIBLE: M_SIZE_NEGLIGIBLE,
        SizeBucket.HIGH: M_SIZE_HIGH,
        SizeBucket.NORMAL: 1.0,
        SizeBucket.UNKNOWN: 1.0,
    }[size_bucket]
    mult["cfo"] = M_CFO if has_cfo else 1.0
    mult["tier"] = M_TIER.get(best_tier, 1.0)
    mult["rnd"] = M_RND_HIGH if rnd_amplified else 1.0

    composite = 1.0
    for v in mult.values():
        composite *= v
    composite = round(composite, 4)

    if composite >= LEVEL_HIGH_AT:
        level = ConvictionLevel.HIGH
    elif composite >= LEVEL_MEDIUM_AT:
        level = ConvictionLevel.MEDIUM
    else:
        level = ConvictionLevel.LOW

    rationale = (
        f"{n_distinct} insider(s){' [cluster]' if is_cluster else ''}, "
        f"size {size_bucket.value}"
        + (f" ({pct*100:.4f}% SO)" if pct is not None else "")
        + f", best tier {best_tier}"
        + (", CFO buy" if has_cfo else "")
        + (", high-R&D" if rnd_amplified else "")
        + f" → composite {composite} → {level.value}"
    )

    return ConvictionScore(
        ticker=ticker, event_date=ev_date, n_events=len(events),
        n_effective_events=len(effective), n_distinct_insiders=n_distinct,
        is_cluster=is_cluster, total_shares=total_shares, total_value=round(total_value, 2),
        shares_outstanding=shares_outstanding,
        pct_shares_outstanding=round(pct, 8) if pct is not None else None,
        size_bucket=size_bucket.value, has_cfo=has_cfo, best_tier=best_tier,
        rnd_intensity=rnd_intensity, rnd_amplified=rnd_amplified,
        multipliers=mult, composite_score=composite, level=level.value,
        rationale=rationale, excluded_ten_pct_only=excluded,
    )


_TIER_ORDER = ["elite", "good", "neutral", "unrated", "poor"]


def _best_tier(tiers: list[str]) -> str:
    """Best (most favorable) tier present. elite > good > neutral > unrated > poor."""
    for t in _TIER_ORDER:
        if t in tiers:
            return t
    return "unrated"
