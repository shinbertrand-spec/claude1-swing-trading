"""Cashtag-driven X scanner for the news-research hourly snapshot.

Sibling to :mod:`tools.thematic_portfolio.corpus.x_ingest` (handle-driven).
Per ``Bertieboo/wiki/notes/swing-news-research-x-scanner-design-spec.md``:

* **Trigger:** runs once per hour during US market hours as part of the
  news-research firing pipeline.
* **Inputs:** all tickers in ``journal/watchlist.json`` + all tickers in
  ``journal/positions.json`` (no capital risk on watchlist; positions
  carry risk — both worth monitoring for sentiment + breaking news).
* **Query pattern:** for each ticker, fetch
  ``$<TICKER> -is:retweet lang:en`` via twitterapi.io's
  ``advanced_search`` endpoint.
* **Output:** the ``x_signals[]`` block of the hourly news-snapshot YAML
  (the schema additions land in Step 4 of the build order; this module's
  output dataclasses already match the canonical shape).

## Three-stage material filter

Cost discipline: filters most noise out before the LLM cost path.

**Stage 1 — hard floors (deterministic, free):**
* engagement: likes ≥ 100 OR retweets ≥ 20 OR quotes ≥ 10
* author_followers ≥ 1000 (rough quality floor)
* lang == "en"
* NOT a retweet (already handled in query but defense-in-depth)
* Created within the past hour from snapshot top-of-hour

**Stage 2 — LLM material classification (Haiku, ~$0.0003 per call):**
For posts passing Stage 1, the news-research subagent invokes a Haiku
classifier. The classifier returns ``ClassificationVerdict`` with
``material: bool``, ``sentiment_tag``, ``named_themes``.

This module does NOT call the LLM itself — the subagent invokes it via
an ``Agent`` tool call and feeds the result back through the
``classifier_callable`` argument. Same DI pattern as
:mod:`tools.thematic_portfolio.artifact_classifier`.

**Stage 3 — top-N cap per ticker (cost discipline):**
At most 5 material signals per ticker per hour. If more pass Stages 1+2,
rank by total engagement (likes + retweets + quotes + replies) and keep
the top 5.

## Cross-consumer reference

When the same tweet was also ingested by the thematic side (e.g., an
@leopoldasch post that names $NVDA), the swing signal's
``cross_consumer_ref.thematic_ledger_path`` points at the per-post
artifact in ``ledgers/thematic/corpus/x/<date>/<post_id>.yml``. Cheap
file-existence check; no API calls.

## Cost projection (per design spec § 6)

* 20 tickers × 154 market hours/month = 3,080 queries
* ~5 tweets returned per query × $0.00015 = **~$2.30/month** API
* Stage 1 pass-through: 10-20% → ~2,000 classifier calls × $0.0003 =
  **~$0.60/month** LLM
* Combined swing-side: **~$3/month**, well inside budget envelope.

This module enforces NO budget cap — the news-research subagent /
caller is responsible for cost monitoring. The deterministic Stage 1
hard-floors are the operational cost discipline.

## CLI

::

    # Scan one ticker; subagent invokes for hourly snapshot composition.
    uv run python -m tools.news_research.x_scanner \\
        --tickers NVDA,AMD,VRT \\
        --snapshot-top-of-hour 2026-05-25T18:00:00+00:00 \\
        --max-pages-per-ticker 2 \\
        --dry-run

In ``--dry-run`` mode the classifier is skipped; only Stage 1 + a
``classifier_result: null`` placeholder is emitted. Production runs
pass classifier verdicts via the subagent pipeline (a CLI flag for
batch-classified-via-jsonl could land later if needed).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from ..cli import emit
from ..contract import TraceEntry
from ..x_common.twitterapi_client import TwitterAPIClient

TOOL = "tools/news_research/x_scanner.py"

DEFAULT_THEMATIC_X_ROOT = Path("ledgers/thematic/corpus/x")
DEFAULT_WATCHLIST_PATH = Path("journal/watchlist.json")
DEFAULT_POSITIONS_PATH = Path("journal/positions.json")


# ---------------------------------------------------------------------------
# Stage 1 — deterministic hard floors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stage1Floors:
    """V1 starting values per design spec § 5.

    Tune from scorecard data later. Held as a struct so callers can
    override for testing or progressive tightening.
    """

    min_likes: int = 100
    min_retweets: int = 20
    min_quotes: int = 10
    min_author_followers: int = 1000
    max_age_minutes: int = 60
    require_language: str = "en"


@dataclass
class Stage1Result:
    """Outcome of Stage 1 for one tweet."""

    pass_stage1: bool
    rejection_reason: str | None = None


def _parse_tweet_created_at(value: Any) -> datetime | None:
    """Tolerant timestamp parser.

    twitterapi.io's ``createdAt`` field is RFC 2822 in most responses
    (``"Wed Oct 08 20:19:20 +0000 2025"``); ISO-8601 in some. Returns
    None when both fail.
    """
    if not value or not isinstance(value, str):
        return None
    # Try ISO-8601 first
    try:
        cleaned = value.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        return datetime.fromisoformat(cleaned)
    except ValueError:
        pass
    # Fall back to RFC 2822
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def passes_stage1(
    tweet: dict[str, Any],
    *,
    snapshot_top_of_hour: datetime,
    floors: Stage1Floors = Stage1Floors(),
) -> Stage1Result:
    """Apply deterministic Stage 1 filter to one tweet.

    Returns a :class:`Stage1Result` with ``pass_stage1: True`` ONLY when
    every floor passes. The first floor to fail short-circuits +
    populates ``rejection_reason``.
    """
    # Language gate
    lang = tweet.get("lang") or tweet.get("language") or ""
    if lang and floors.require_language and lang != floors.require_language:
        return Stage1Result(False, f"language_{lang}_not_{floors.require_language}")

    # Retweet-of-no-comment gate (defense-in-depth; query already excludes)
    if tweet.get("isRetweet") is True or tweet.get("retweeted_status"):
        return Stage1Result(False, "is_retweet_without_comment")

    # Engagement gate — at least ONE of the three engagement floors clears
    likes = int(tweet.get("likeCount") or 0)
    retweets = int(tweet.get("retweetCount") or 0)
    quotes = int(tweet.get("quoteCount") or 0)
    engagement_clears = (
        likes >= floors.min_likes
        or retweets >= floors.min_retweets
        or quotes >= floors.min_quotes
    )
    if not engagement_clears:
        return Stage1Result(
            False,
            f"engagement_below_floor(likes={likes},rts={retweets},quotes={quotes})",
        )

    # Author followers gate
    author = tweet.get("author") or {}
    followers = int(
        author.get("followers")
        or author.get("followersCount")
        or author.get("public_metrics", {}).get("followers_count")
        or 0
    )
    if followers < floors.min_author_followers:
        return Stage1Result(False, f"author_followers_{followers}_below_floor")

    # Recency gate — past hour from snapshot top-of-hour
    created = _parse_tweet_created_at(
        tweet.get("createdAt") or tweet.get("created_at")
    )
    if created is None:
        return Stage1Result(False, "unparseable_created_at")
    # Normalise both to aware UTC for comparison.
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if snapshot_top_of_hour.tzinfo is None:
        snapshot_top_of_hour = snapshot_top_of_hour.replace(tzinfo=timezone.utc)
    age = snapshot_top_of_hour - created
    if age > timedelta(minutes=floors.max_age_minutes):
        return Stage1Result(False, f"age_{int(age.total_seconds()/60)}min_exceeds_floor")
    if age < timedelta(minutes=-5):
        # Future-dated by more than 5 minutes — likely a clock-skew oddity.
        return Stage1Result(False, "future_timestamp")

    return Stage1Result(True, None)


# ---------------------------------------------------------------------------
# Stage 2 — LLM classifier (DI pattern; module is just the data shape)
# ---------------------------------------------------------------------------


@dataclass
class ClassificationVerdict:
    """Stage 2 LLM verdict for one tweet.

    The news-research subagent invokes the Haiku classifier and passes
    the result back via :func:`compose`'s ``classifier_callable``
    argument. Tests inject a stub callable; production runs go through
    the subagent's ``Agent`` tool call.
    """

    material: bool
    sentiment_tag: str          # bullish / bearish / neutral / breaking-news
    named_themes: list[str]
    rationale: str
    classifier_model: str = "claude-haiku-4-5-20251001"
    classifier_cost_usd: float = 0.0003
    classified_at: str | None = None


VALID_SENTIMENT_TAGS = frozenset(
    {"bullish", "bearish", "neutral", "breaking-news"}
)


# A callable that classifies one (tweet, cashtag) pair. Return None to
# indicate the classifier was skipped (e.g. dry-run); the signal then
# carries ``classifier_result: null`` and is treated as material=True
# by default so it survives Stage 3 for review.
ClassifierCallable = Callable[[dict, str], "ClassificationVerdict | None"]


# ---------------------------------------------------------------------------
# Stage 3 — top-N cap per ticker
# ---------------------------------------------------------------------------


def _engagement_score(tweet: dict[str, Any]) -> int:
    """Sum-of-engagements score used for Stage 3 ranking."""
    return (
        int(tweet.get("likeCount") or 0)
        + int(tweet.get("retweetCount") or 0)
        + int(tweet.get("quoteCount") or 0)
        + int(tweet.get("replyCount") or 0)
    )


def apply_stage3_top_n(
    signals: list["XSignal"], *, n_per_ticker: int = 5,
) -> list["XSignal"]:
    """Cap at ``n_per_ticker`` material signals per ticker.

    Within each ticker bucket, drop signals where ``material is False``
    (Stage 2 said no) before applying the top-N cap. Then sort the
    survivors by ``engagement_score`` desc and keep the top N.
    """
    by_ticker: dict[str, list[XSignal]] = {}
    for sig in signals:
        if sig.classifier_result is not None and not sig.classifier_result.get(
            "material", True
        ):
            continue
        by_ticker.setdefault(sig.cashtag, []).append(sig)
    out: list[XSignal] = []
    for ticker, bucket in by_ticker.items():
        bucket.sort(key=lambda s: -s.engagement_score)
        out.extend(bucket[:n_per_ticker])
    # Stable secondary sort by ticker for deterministic output ordering
    out.sort(key=lambda s: (s.cashtag, -s.engagement_score))
    return out


# ---------------------------------------------------------------------------
# XSignal — the per-tweet record that lands in the snapshot's x_signals[]
# ---------------------------------------------------------------------------


@dataclass
class XSignal:
    """One material tweet, fully composed for the news-snapshot YAML.

    Mirrors the shape documented in the design spec § 4.
    """

    tweet_id: str
    author_username: str
    author_followers: int
    cashtag: str
    created_at: str | None
    text: str
    in_reply_to: str | None
    quote_post_id: str | None
    quote_post_excerpt: str | None
    has_media: bool
    url: str
    engagement: dict[str, Any]
    classifier_result: dict[str, Any] | None
    cross_consumer_ref: dict[str, Any]
    engagement_score: int = 0

    def to_dict(self) -> dict[str, Any]:
        # engagement_score is an internal sort key; not part of the
        # documented schema.
        d = asdict(self)
        d.pop("engagement_score", None)
        return d


def _build_signal(
    *,
    tweet: dict[str, Any],
    cashtag: str,
    verdict: ClassificationVerdict | None,
    fetched_at_iso: str,
    thematic_ledger_path: str | None = None,
) -> XSignal:
    """Compose an :class:`XSignal` from a raw tweet + classifier verdict."""
    tweet_id = str(tweet.get("id") or tweet.get("tweetId") or "")
    author = tweet.get("author") or {}
    author_username = (
        author.get("userName") or author.get("screen_name") or author.get("username") or ""
    )
    author_followers = int(
        author.get("followers")
        or author.get("followersCount")
        or author.get("public_metrics", {}).get("followers_count")
        or 0
    )

    # Engagement block
    engagement = {
        "reply_count": int(tweet.get("replyCount") or 0),
        "retweet_count": int(tweet.get("retweetCount") or 0),
        "quote_count": int(tweet.get("quoteCount") or 0),
        "like_count": int(tweet.get("likeCount") or 0),
        "view_count": (
            int(tweet["viewCount"]) if tweet.get("viewCount") is not None else None
        ),
        "fetched_at": fetched_at_iso,
    }

    # Quote-tweet excerpt (if any)
    quote_obj = tweet.get("quoted_tweet") or tweet.get("quotedTweet") or {}
    quote_post_id = (
        tweet.get("quoteId")
        or tweet.get("quotedStatusId")
        or quote_obj.get("id")
    )
    quote_excerpt = (quote_obj or {}).get("text") if quote_obj else None

    in_reply_to = (
        tweet.get("inReplyToId")
        or tweet.get("in_reply_to_status_id")
        or tweet.get("in_reply_to_post_id")
    )

    # has_media detection (mirrors x_ingest)
    entities = tweet.get("entities") or {}
    media = entities.get("media") or tweet.get("media") or tweet.get("mediaUrls")
    has_media = bool(media)

    classifier_block: dict[str, Any] | None = None
    if verdict is not None:
        classifier_block = {
            "material": verdict.material,
            "rationale": verdict.rationale,
            "sentiment_tag": verdict.sentiment_tag,
            "named_themes": list(verdict.named_themes),
            "classifier_model": verdict.classifier_model,
            "classifier_cost_usd": verdict.classifier_cost_usd,
            "classified_at": verdict.classified_at or fetched_at_iso,
        }

    return XSignal(
        tweet_id=tweet_id,
        author_username=author_username,
        author_followers=author_followers,
        cashtag=cashtag,
        created_at=tweet.get("createdAt") or tweet.get("created_at"),
        text=tweet.get("text") or "",
        in_reply_to=str(in_reply_to) if in_reply_to else None,
        quote_post_id=str(quote_post_id) if quote_post_id else None,
        quote_post_excerpt=quote_excerpt,
        has_media=has_media,
        url=tweet.get("url") or f"https://x.com/{author_username}/status/{tweet_id}",
        engagement=engagement,
        classifier_result=classifier_block,
        cross_consumer_ref={"thematic_ledger_path": thematic_ledger_path},
        engagement_score=_engagement_score(tweet),
    )


# ---------------------------------------------------------------------------
# Cross-consumer reference helper
# ---------------------------------------------------------------------------


def _resolve_thematic_ledger_path(
    tweet_id: str, *, thematic_x_root: Path, tweet_created_at: str | None,
) -> str | None:
    """File-existence check for a thematic x_ingest artifact at the same tweet.

    Per the design spec § 4 cross_consumer_ref convention. The thematic
    ingester writes ``<root>/<YYYY-MM-DD>/<tweet_id>.yml``; we know
    ``tweet_id`` directly and can derive ``YYYY-MM-DD`` from
    ``created_at``. Cheap O(1) stat — no API call.
    """
    if not thematic_x_root.exists():
        return None
    # Try the most likely date dir first based on created_at
    created = _parse_tweet_created_at(tweet_created_at) if tweet_created_at else None
    if created is not None:
        candidate = thematic_x_root / created.strftime("%Y-%m-%d") / f"{tweet_id}.yml"
        if candidate.is_file():
            return str(candidate)
    # Fall back to a recursive glob (slow path — only hit if date inference failed)
    matches = list(thematic_x_root.glob(f"*/{tweet_id}.yml"))
    return str(matches[0]) if matches else None


# ---------------------------------------------------------------------------
# Input loaders
# ---------------------------------------------------------------------------


def load_watchlist_tickers(path: Path = DEFAULT_WATCHLIST_PATH) -> list[str]:
    """Read uppercase tickers from ``journal/watchlist.json``."""
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for entry in doc.get("watchlist") or []:
        t = entry.get("ticker")
        if t:
            out.append(t.upper())
    return out


def load_position_tickers(path: Path = DEFAULT_POSITIONS_PATH) -> list[str]:
    """Read uppercase tickers from ``journal/positions.json``."""
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    # positions.json shape: top-level dict with `_schema`, `_comment`,
    # `_position_schema`, plus per-ticker entries. The per-ticker entries
    # are dicts with `ticker` key; reject anything starting with "_".
    tickers: list[str] = []
    if isinstance(doc, dict):
        # Common shape A: {"positions": [{"ticker": "X", ...}, ...]}
        if isinstance(doc.get("positions"), list):
            for entry in doc["positions"]:
                t = entry.get("ticker")
                if t:
                    tickers.append(t.upper())
        # Common shape B: flat top-level dict keyed by ticker
        else:
            for key, val in doc.items():
                if key.startswith("_"):
                    continue
                if isinstance(val, dict) and val.get("ticker"):
                    tickers.append(val["ticker"].upper())
                elif isinstance(val, dict):
                    tickers.append(key.upper())
    return tickers


def union_tickers(*lists: Iterable[str]) -> list[str]:
    """Deduplicated union across multiple ticker lists; deterministic order."""
    seen: set[str] = set()
    out: list[str] = []
    for src in lists:
        for t in src:
            up = t.upper()
            if up not in seen:
                seen.add(up)
                out.append(up)
    return out


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


@dataclass
class TickerScanResult:
    ticker: str
    fetched: int = 0
    passed_stage1: int = 0
    passed_stage2: int = 0
    emitted: int = 0
    error: str | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_top_of_hour(value: str | datetime | None) -> datetime:
    """Resolve the snapshot top-of-hour timestamp."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value:
        cleaned = value.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    # Default = the most-recent top-of-hour
    return now.replace(minute=0, second=0, microsecond=0)


def compose(
    *,
    tickers: list[str],
    classifier_callable: ClassifierCallable | None = None,
    client: TwitterAPIClient | None = None,
    snapshot_top_of_hour: str | datetime | None = None,
    floors: Stage1Floors = Stage1Floors(),
    n_per_ticker: int = 5,
    max_pages_per_ticker: int = 2,
    thematic_x_root: Path = DEFAULT_THEMATIC_X_ROOT,
    now_iso_fn: Callable[[], str] = _utc_now_iso,
) -> TraceEntry:
    """Run one full x-scan cycle across the given ticker list.

    Args:
        tickers: list of ticker symbols. Caller is responsible for
            deduping + uppercasing (or use :func:`union_tickers`).
        classifier_callable: per-tweet classifier. When None, no Stage 2
            calls are made and signals carry ``classifier_result: null``;
            useful for dry-runs.
        client: TwitterAPIClient — created on demand when omitted.
        snapshot_top_of_hour: snapshot anchor for the recency floor.
            Defaults to the current top-of-hour.
        floors: override the deterministic floors (tests).
        n_per_ticker: Stage 3 cap.
        max_pages_per_ticker: pagination cap.
        thematic_x_root: where to look for cross_consumer_ref matches.
        now_iso_fn: clock injection.

    Returns:
        TraceEntry whose ``output`` summarises the cycle + carries the
        composed ``x_signals[]`` list.
    """
    fetched_at_iso = now_iso_fn()
    top_of_hour_dt = _parse_top_of_hour(snapshot_top_of_hour)
    client = client or TwitterAPIClient()

    per_ticker_results: list[TickerScanResult] = []
    composed_signals: list[XSignal] = []

    for ticker in tickers:
        tres = TickerScanResult(ticker=ticker)
        try:
            query = f"${ticker} -is:retweet lang:en"
            for tweet in client.iter_advanced_search(
                query=query, query_type="Latest", max_pages=max_pages_per_ticker,
            ):
                tres.fetched += 1
                stage1 = passes_stage1(
                    tweet,
                    snapshot_top_of_hour=top_of_hour_dt,
                    floors=floors,
                )
                if not stage1.pass_stage1:
                    continue
                tres.passed_stage1 += 1

                verdict: ClassificationVerdict | None = None
                if classifier_callable is not None:
                    verdict = classifier_callable(tweet, ticker)
                    if verdict is not None and verdict.material:
                        tres.passed_stage2 += 1
                    elif verdict is None:
                        # Treat None as "skipped" — still survives to Stage 3
                        tres.passed_stage2 += 1
                else:
                    tres.passed_stage2 += 1

                tweet_id = str(tweet.get("id") or tweet.get("tweetId") or "")
                thematic_path = _resolve_thematic_ledger_path(
                    tweet_id,
                    thematic_x_root=thematic_x_root,
                    tweet_created_at=tweet.get("createdAt")
                    or tweet.get("created_at"),
                )
                signal = _build_signal(
                    tweet=tweet,
                    cashtag=f"${ticker}",
                    verdict=verdict,
                    fetched_at_iso=fetched_at_iso,
                    thematic_ledger_path=thematic_path,
                )
                composed_signals.append(signal)
        except Exception as e:  # noqa: BLE001 — per-ticker isolation
            tres.error = f"{type(e).__name__}: {e}"
        per_ticker_results.append(tres)

    # Stage 3 — top-N cap per ticker
    capped = apply_stage3_top_n(
        composed_signals, n_per_ticker=n_per_ticker,
    )

    # Reconcile emit counters per ticker post-cap
    by_ticker_count: dict[str, int] = {}
    for s in capped:
        bare = s.cashtag.lstrip("$")
        by_ticker_count[bare] = by_ticker_count.get(bare, 0) + 1
    for tres in per_ticker_results:
        tres.emitted = by_ticker_count.get(tres.ticker, 0)

    payload = {
        "fetched_at": fetched_at_iso,
        "snapshot_top_of_hour": top_of_hour_dt.isoformat(timespec="seconds"),
        "n_tickers_scanned": len(tickers),
        "n_signals_emitted": len(capped),
        "n_errors": sum(1 for r in per_ticker_results if r.error),
        "per_ticker": [asdict(r) for r in per_ticker_results],
        "x_signals": [s.to_dict() for s in capped],
    }

    return TraceEntry(
        tool=TOOL,
        inputs={
            "n_tickers": len(tickers),
            "n_per_ticker_cap": n_per_ticker,
            "max_pages_per_ticker": max_pages_per_ticker,
            "snapshot_top_of_hour": top_of_hour_dt.isoformat(timespec="seconds"),
            "classifier_provided": classifier_callable is not None,
        },
        output=payload,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tools.news_research.x_scanner",
        description=__doc__,
    )
    p.add_argument(
        "--tickers",
        help="Comma-separated ticker list. When omitted, loads from "
        "--watchlist + --positions union.",
    )
    p.add_argument(
        "--watchlist",
        default=str(DEFAULT_WATCHLIST_PATH),
        help=f"Path to watchlist.json (default {DEFAULT_WATCHLIST_PATH}).",
    )
    p.add_argument(
        "--positions",
        default=str(DEFAULT_POSITIONS_PATH),
        help=f"Path to positions.json (default {DEFAULT_POSITIONS_PATH}).",
    )
    p.add_argument(
        "--snapshot-top-of-hour",
        help="ISO-8601 timestamp anchoring the recency floor. "
        "Defaults to current top-of-hour.",
    )
    p.add_argument(
        "--max-pages-per-ticker", type=int, default=2,
        help="Pagination cap per ticker (default 2 = up to 40 tweets/ticker).",
    )
    p.add_argument(
        "--n-per-ticker", type=int, default=5,
        help="Stage 3 cap — material tweets per ticker (default 5).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Skip the Stage 2 LLM classifier; emit signals with classifier_result: null.",
    )
    return p


def main() -> int:
    args = _build_arg_parser().parse_args()

    if args.tickers:
        tickers = union_tickers([t.strip() for t in args.tickers.split(",") if t.strip()])
    else:
        tickers = union_tickers(
            load_watchlist_tickers(Path(args.watchlist)),
            load_position_tickers(Path(args.positions)),
        )

    trace = compose(
        tickers=tickers,
        classifier_callable=None,  # CLI = dry-run-equivalent; subagent supplies
        snapshot_top_of_hour=args.snapshot_top_of_hour,
        n_per_ticker=args.n_per_ticker,
        max_pages_per_ticker=args.max_pages_per_ticker,
    )
    emit(trace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
