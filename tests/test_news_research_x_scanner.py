"""Tests for tools.news_research.x_scanner."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.news_research.x_scanner import (
    DEFAULT_THEMATIC_X_ROOT,
    ClassificationVerdict,
    Stage1Floors,
    Stage1Result,
    XSignal,
    _engagement_score,
    _parse_tweet_created_at,
    _parse_top_of_hour,
    _resolve_thematic_ledger_path,
    apply_stage3_top_n,
    compose,
    load_position_tickers,
    load_watchlist_tickers,
    passes_stage1,
    union_tickers,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


TOP_OF_HOUR = datetime(2026, 5, 25, 18, 0, 0, tzinfo=timezone.utc)


def _tweet(
    *,
    id="100",
    text="$NVDA pulling back to 50-day MA on declining volume",
    created_at=None,
    author_username="QullamagieDave",
    author_followers=234000,
    likes=1200,
    retweets=89,
    quotes=12,
    replies=23,
    views=80000,
    lang="en",
    is_retweet=False,
    in_reply_to=None,
    quote_obj=None,
):
    if created_at is None:
        created_at = (TOP_OF_HOUR - timedelta(minutes=30)).strftime(
            "%a %b %d %H:%M:%S +0000 %Y"
        )
    t: dict = {
        "id": id,
        "text": text,
        "createdAt": created_at,
        "author": {"userName": author_username, "followers": author_followers},
        "likeCount": likes,
        "retweetCount": retweets,
        "quoteCount": quotes,
        "replyCount": replies,
        "viewCount": views,
        "lang": lang,
    }
    if is_retweet:
        t["isRetweet"] = True
    if in_reply_to:
        t["inReplyToId"] = in_reply_to
    if quote_obj:
        t["quoted_tweet"] = quote_obj
    return t


class _StubClient:
    """Returns canned tweet pages keyed by cashtag (e.g. '$NVDA')."""

    def __init__(self, tweets_by_cashtag, raise_on_cashtag=None):
        self._tweets = tweets_by_cashtag
        self._raise = raise_on_cashtag or {}
        self.calls: list[str] = []

    def iter_advanced_search(self, *, query, query_type="Latest", max_pages=2):
        self.calls.append(query)
        # Extract cashtag from query like "$NVDA -is:retweet lang:en"
        cashtag = None
        for tok in query.split():
            if tok.startswith("$"):
                cashtag = tok
                break
        if cashtag and cashtag in self._raise:
            raise self._raise[cashtag]
        yield from self._tweets.get(cashtag, [])


def _verdict(material=True, sentiment="bullish", themes=None, rationale="ok"):
    return ClassificationVerdict(
        material=material,
        sentiment_tag=sentiment,
        named_themes=themes or [],
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# _parse_tweet_created_at
# ---------------------------------------------------------------------------


def test_parse_iso_8601_with_z():
    dt = _parse_tweet_created_at("2026-05-25T18:30:00Z")
    assert dt == datetime(2026, 5, 25, 18, 30, tzinfo=timezone.utc)


def test_parse_iso_8601_with_offset():
    dt = _parse_tweet_created_at("2026-05-25T18:30:00+00:00")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_rfc_2822_format():
    dt = _parse_tweet_created_at("Wed Oct 08 20:19:20 +0000 2025")
    assert dt is not None
    assert dt.year == 2025 and dt.month == 10 and dt.day == 8


def test_parse_returns_none_on_garbage():
    assert _parse_tweet_created_at("not a date") is None
    assert _parse_tweet_created_at(None) is None
    assert _parse_tweet_created_at("") is None


# ---------------------------------------------------------------------------
# Stage 1 floors — engagement
# ---------------------------------------------------------------------------


def test_stage1_passes_when_likes_clear_floor():
    res = passes_stage1(
        _tweet(likes=150, retweets=0, quotes=0),
        snapshot_top_of_hour=TOP_OF_HOUR,
    )
    assert res.pass_stage1


def test_stage1_passes_when_retweets_clear_floor():
    res = passes_stage1(
        _tweet(likes=0, retweets=25, quotes=0),
        snapshot_top_of_hour=TOP_OF_HOUR,
    )
    assert res.pass_stage1


def test_stage1_passes_when_quotes_clear_floor():
    res = passes_stage1(
        _tweet(likes=0, retweets=0, quotes=15),
        snapshot_top_of_hour=TOP_OF_HOUR,
    )
    assert res.pass_stage1


def test_stage1_rejects_when_all_engagement_below_floor():
    res = passes_stage1(
        _tweet(likes=10, retweets=2, quotes=1),
        snapshot_top_of_hour=TOP_OF_HOUR,
    )
    assert not res.pass_stage1
    assert "engagement_below_floor" in res.rejection_reason


# ---------------------------------------------------------------------------
# Stage 1 floors — author / language / retweet / recency
# ---------------------------------------------------------------------------


def test_stage1_rejects_low_follower_author():
    res = passes_stage1(
        _tweet(author_followers=50),
        snapshot_top_of_hour=TOP_OF_HOUR,
    )
    assert not res.pass_stage1
    assert "author_followers" in res.rejection_reason


def test_stage1_rejects_non_english_when_floor_enabled():
    res = passes_stage1(
        _tweet(lang="es"),
        snapshot_top_of_hour=TOP_OF_HOUR,
    )
    assert not res.pass_stage1
    assert "language_es" in res.rejection_reason


def test_stage1_rejects_retweet_without_comment():
    res = passes_stage1(
        _tweet(is_retweet=True),
        snapshot_top_of_hour=TOP_OF_HOUR,
    )
    assert not res.pass_stage1
    assert "retweet" in res.rejection_reason


def test_stage1_rejects_old_tweet():
    """A tweet older than max_age_minutes (60 default) MUST fail."""
    old_created = (TOP_OF_HOUR - timedelta(minutes=120)).strftime(
        "%a %b %d %H:%M:%S +0000 %Y"
    )
    res = passes_stage1(
        _tweet(created_at=old_created),
        snapshot_top_of_hour=TOP_OF_HOUR,
    )
    assert not res.pass_stage1
    assert "age" in res.rejection_reason


def test_stage1_rejects_unparseable_timestamp():
    res = passes_stage1(
        _tweet(created_at="banana"),
        snapshot_top_of_hour=TOP_OF_HOUR,
    )
    assert not res.pass_stage1
    assert "unparseable" in res.rejection_reason


def test_stage1_accepts_iso_8601_timestamps():
    iso = (TOP_OF_HOUR - timedelta(minutes=30)).isoformat()
    res = passes_stage1(
        _tweet(created_at=iso),
        snapshot_top_of_hour=TOP_OF_HOUR,
    )
    assert res.pass_stage1


def test_stage1_rejects_future_dated_tweets_by_more_than_5_min():
    future = (TOP_OF_HOUR + timedelta(minutes=30)).isoformat()
    res = passes_stage1(
        _tweet(created_at=future),
        snapshot_top_of_hour=TOP_OF_HOUR,
    )
    assert not res.pass_stage1
    assert "future" in res.rejection_reason


def test_stage1_tolerates_5min_clock_skew():
    """Allow up to 5 minutes future-dating for clock-skew tolerance."""
    skewed = (TOP_OF_HOUR + timedelta(minutes=2)).isoformat()
    res = passes_stage1(
        _tweet(created_at=skewed),
        snapshot_top_of_hour=TOP_OF_HOUR,
    )
    assert res.pass_stage1


def test_stage1_floors_configurable_per_call():
    """Loosened floors should let lower-engagement tweets through."""
    floors = Stage1Floors(min_likes=10, min_retweets=2, min_quotes=1, min_author_followers=100)
    res = passes_stage1(
        _tweet(likes=15, retweets=3, author_followers=200),
        snapshot_top_of_hour=TOP_OF_HOUR,
        floors=floors,
    )
    assert res.pass_stage1


# ---------------------------------------------------------------------------
# Stage 3 — top-N cap per ticker
# ---------------------------------------------------------------------------


def _signal(*, ticker="NVDA", engagement=100, material=True, sentiment="bullish"):
    return XSignal(
        tweet_id="x",
        author_username="a",
        author_followers=0,
        cashtag=f"${ticker}",
        created_at=None,
        text="",
        in_reply_to=None,
        quote_post_id=None,
        quote_post_excerpt=None,
        has_media=False,
        url="",
        engagement={},
        classifier_result={"material": material, "sentiment_tag": sentiment},
        cross_consumer_ref={"thematic_ledger_path": None},
        engagement_score=engagement,
    )


def test_stage3_caps_at_n_per_ticker():
    signals = [_signal(engagement=i) for i in range(10)]
    capped = apply_stage3_top_n(signals, n_per_ticker=5)
    assert len(capped) == 5


def test_stage3_keeps_highest_engagement_within_bucket():
    signals = [_signal(engagement=i) for i in (3, 100, 50, 7, 200, 1)]
    capped = apply_stage3_top_n(signals, n_per_ticker=3)
    assert [s.engagement_score for s in capped] == [200, 100, 50]


def test_stage3_independent_buckets_per_ticker():
    nvda = [_signal(ticker="NVDA", engagement=e) for e in (100, 200, 300)]
    amd = [_signal(ticker="AMD", engagement=e) for e in (50, 60, 70, 80, 90, 95)]
    capped = apply_stage3_top_n(nvda + amd, n_per_ticker=2)
    by_ticker: dict[str, list[XSignal]] = {}
    for s in capped:
        by_ticker.setdefault(s.cashtag, []).append(s)
    assert len(by_ticker["$NVDA"]) == 2
    assert len(by_ticker["$AMD"]) == 2


def test_stage3_drops_material_false_signals():
    signals = [
        _signal(engagement=300, material=False),  # dropped
        _signal(engagement=100, material=True),
    ]
    capped = apply_stage3_top_n(signals, n_per_ticker=5)
    assert len(capped) == 1
    assert capped[0].engagement_score == 100


def test_stage3_keeps_signals_with_null_classifier():
    """classifier_result=None means dry-run-skipped; treat as material."""
    sig = _signal()
    sig.classifier_result = None
    capped = apply_stage3_top_n([sig], n_per_ticker=5)
    assert len(capped) == 1


# ---------------------------------------------------------------------------
# _engagement_score
# ---------------------------------------------------------------------------


def test_engagement_score_sums_all_buckets():
    score = _engagement_score(_tweet(likes=10, retweets=5, quotes=2, replies=3))
    assert score == 20


def test_engagement_score_handles_missing_keys():
    assert _engagement_score({}) == 0


# ---------------------------------------------------------------------------
# Cross-consumer ref resolution
# ---------------------------------------------------------------------------


def test_resolve_thematic_ledger_path_finds_match(tmp_path):
    root = tmp_path / "x"
    (root / "2026-05-25").mkdir(parents=True)
    (root / "2026-05-25" / "100.yml").write_text("---")
    p = _resolve_thematic_ledger_path(
        "100",
        thematic_x_root=root,
        tweet_created_at=(TOP_OF_HOUR - timedelta(minutes=30)).strftime(
            "%a %b %d %H:%M:%S +0000 %Y"
        ),
    )
    assert p is not None
    assert p.endswith("100.yml")


def test_resolve_thematic_ledger_path_returns_none_when_no_match(tmp_path):
    root = tmp_path / "x"
    root.mkdir()
    assert _resolve_thematic_ledger_path(
        "100",
        thematic_x_root=root,
        tweet_created_at=(TOP_OF_HOUR - timedelta(minutes=30)).strftime(
            "%a %b %d %H:%M:%S +0000 %Y"
        ),
    ) is None


def test_resolve_thematic_ledger_path_returns_none_when_root_missing(tmp_path):
    assert _resolve_thematic_ledger_path(
        "100",
        thematic_x_root=tmp_path / "nope",
        tweet_created_at="x",
    ) is None


def test_resolve_thematic_ledger_path_falls_back_to_recursive_glob(tmp_path):
    """When created_at can't be parsed but the file exists somewhere
    in the root tree, the slow-path glob still finds it."""
    root = tmp_path / "x"
    (root / "2026-05-26").mkdir(parents=True)
    (root / "2026-05-26" / "100.yml").write_text("---")
    p = _resolve_thematic_ledger_path(
        "100",
        thematic_x_root=root,
        tweet_created_at="garbage",
    )
    assert p is not None


# ---------------------------------------------------------------------------
# Input loaders
# ---------------------------------------------------------------------------


def test_load_watchlist_tickers_returns_uppercase(tmp_path):
    p = tmp_path / "watchlist.json"
    p.write_text(json.dumps({
        "watchlist": [
            {"ticker": "nvda"},
            {"ticker": "VRT"},
            {"ticker": "amd"},
        ],
    }))
    assert load_watchlist_tickers(p) == ["NVDA", "VRT", "AMD"]


def test_load_watchlist_tickers_empty_when_missing(tmp_path):
    assert load_watchlist_tickers(tmp_path / "nope.json") == []


def test_load_watchlist_skips_entries_without_ticker(tmp_path):
    p = tmp_path / "watchlist.json"
    p.write_text(json.dumps({
        "watchlist": [{"ticker": "NVDA"}, {"comment_only": True}],
    }))
    assert load_watchlist_tickers(p) == ["NVDA"]


def test_load_position_tickers_flat_top_level_shape(tmp_path):
    p = tmp_path / "positions.json"
    p.write_text(json.dumps({
        "_schema": "v2",
        "_comment": "ignored",
        "_position_schema": {"ticker": "string"},
        "NVDA": {"ticker": "NVDA", "shares": 100},
        "VRT": {"ticker": "VRT", "shares": 50},
    }))
    out = load_position_tickers(p)
    assert set(out) == {"NVDA", "VRT"}


def test_load_position_tickers_positions_array_shape(tmp_path):
    """Alternative shape: positions key with an array."""
    p = tmp_path / "positions.json"
    p.write_text(json.dumps({
        "positions": [
            {"ticker": "NVDA", "shares": 100},
            {"ticker": "VRT", "shares": 50},
        ],
    }))
    assert set(load_position_tickers(p)) == {"NVDA", "VRT"}


def test_load_position_tickers_skips_underscore_keys(tmp_path):
    p = tmp_path / "positions.json"
    p.write_text(json.dumps({
        "_schema": "v2",
        "_comment": "ignored",
        "NVDA": {"ticker": "NVDA"},
    }))
    assert "_schema" not in load_position_tickers(p)


def test_union_tickers_dedups_and_uppercases():
    a = ["NVDA", "amd", "VRT"]
    b = ["AMD", "TSLA"]
    assert union_tickers(a, b) == ["NVDA", "AMD", "VRT", "TSLA"]


# ---------------------------------------------------------------------------
# compose — full cycle
# ---------------------------------------------------------------------------


def test_compose_emits_signals_for_passing_tweets(tmp_path):
    client = _StubClient(tweets_by_cashtag={
        "$NVDA": [_tweet(id="100"), _tweet(id="101", likes=5, retweets=1, quotes=0)],
    })
    trace = compose(
        tickers=["NVDA"],
        client=client,
        classifier_callable=lambda tweet, cashtag: _verdict(material=True),
        snapshot_top_of_hour=TOP_OF_HOUR,
        thematic_x_root=tmp_path / "x",
        now_iso_fn=lambda: "2026-05-25T18:00:00+00:00",
    )
    out = trace.output
    # Tweet "100" passes Stage 1 (engagement clears); "101" rejected (low eng)
    assert out["n_signals_emitted"] == 1
    assert out["x_signals"][0]["tweet_id"] == "100"
    assert out["per_ticker"][0]["fetched"] == 2
    assert out["per_ticker"][0]["passed_stage1"] == 1
    assert out["per_ticker"][0]["emitted"] == 1


def test_compose_skips_classifier_when_callable_is_none(tmp_path):
    client = _StubClient(tweets_by_cashtag={
        "$NVDA": [_tweet(id="100")],
    })
    trace = compose(
        tickers=["NVDA"],
        client=client,
        classifier_callable=None,
        snapshot_top_of_hour=TOP_OF_HOUR,
        thematic_x_root=tmp_path / "x",
        now_iso_fn=lambda: "2026-05-25T18:00:00+00:00",
    )
    out = trace.output
    assert out["x_signals"][0]["classifier_result"] is None
    assert out["x_signals"][0]["tweet_id"] == "100"


def test_compose_per_ticker_isolation_on_api_error(tmp_path):
    from tools.x_common.twitterapi_client import TwitterAPIError
    client = _StubClient(
        tweets_by_cashtag={"$NVDA": [_tweet(id="100")]},
        raise_on_cashtag={"$AMD": TwitterAPIError("503 server")},
    )
    trace = compose(
        tickers=["NVDA", "AMD"],
        client=client,
        classifier_callable=lambda t, c: _verdict(),
        snapshot_top_of_hour=TOP_OF_HOUR,
        thematic_x_root=tmp_path / "x",
        now_iso_fn=lambda: "2026-05-25T18:00:00+00:00",
    )
    out = trace.output
    assert out["n_signals_emitted"] == 1  # NVDA fine, AMD errored
    assert out["n_errors"] == 1
    amd_result = next(r for r in out["per_ticker"] if r["ticker"] == "AMD")
    assert amd_result["error"] is not None


def test_compose_emits_classifier_block_when_verdict_provided(tmp_path):
    client = _StubClient(tweets_by_cashtag={"$NVDA": [_tweet(id="100")]})
    trace = compose(
        tickers=["NVDA"],
        client=client,
        classifier_callable=lambda t, c: _verdict(
            material=True, sentiment="bullish", themes=["stage_2_reset"],
            rationale="strong setup",
        ),
        snapshot_top_of_hour=TOP_OF_HOUR,
        thematic_x_root=tmp_path / "x",
        now_iso_fn=lambda: "2026-05-25T18:00:00+00:00",
    )
    sig = trace.output["x_signals"][0]
    cls = sig["classifier_result"]
    assert cls["material"] is True
    assert cls["sentiment_tag"] == "bullish"
    assert cls["named_themes"] == ["stage_2_reset"]
    assert cls["classifier_model"] == "claude-haiku-4-5-20251001"


def test_compose_top_n_cap_enforced_per_ticker(tmp_path):
    """If 10 tweets pass Stage 1 + 2, only top 5 by engagement land."""
    tweets = [
        _tweet(id=f"id-{i}", likes=1000 + i * 10) for i in range(10)
    ]
    client = _StubClient(tweets_by_cashtag={"$NVDA": tweets})
    trace = compose(
        tickers=["NVDA"],
        client=client,
        classifier_callable=lambda t, c: _verdict(),
        snapshot_top_of_hour=TOP_OF_HOUR,
        thematic_x_root=tmp_path / "x",
        n_per_ticker=5,
        now_iso_fn=lambda: "2026-05-25T18:00:00+00:00",
    )
    out = trace.output
    assert out["n_signals_emitted"] == 5
    assert out["per_ticker"][0]["passed_stage1"] == 10
    assert out["per_ticker"][0]["emitted"] == 5


def test_compose_drops_material_false_signals(tmp_path):
    client = _StubClient(tweets_by_cashtag={"$NVDA": [
        _tweet(id="100"), _tweet(id="200", likes=2000),
    ]})

    def classifier(tweet, cashtag):
        # Tweet 200 is high engagement but classified non-material
        return _verdict(material=(tweet["id"] != "200"))

    trace = compose(
        tickers=["NVDA"],
        client=client,
        classifier_callable=classifier,
        snapshot_top_of_hour=TOP_OF_HOUR,
        thematic_x_root=tmp_path / "x",
        now_iso_fn=lambda: "2026-05-25T18:00:00+00:00",
    )
    ids = [s["tweet_id"] for s in trace.output["x_signals"]]
    assert ids == ["100"]


def test_compose_wires_thematic_ledger_path_when_overlap(tmp_path):
    thematic_root = tmp_path / "x"
    (thematic_root / "2026-05-25").mkdir(parents=True)
    (thematic_root / "2026-05-25" / "100.yml").write_text("---")
    client = _StubClient(tweets_by_cashtag={"$NVDA": [
        _tweet(
            id="100",
            created_at=(TOP_OF_HOUR - timedelta(minutes=30)).strftime(
                "%a %b %d %H:%M:%S +0000 %Y"
            ),
        ),
    ]})
    trace = compose(
        tickers=["NVDA"],
        client=client,
        classifier_callable=lambda t, c: _verdict(),
        snapshot_top_of_hour=TOP_OF_HOUR,
        thematic_x_root=thematic_root,
        now_iso_fn=lambda: "2026-05-25T18:00:00+00:00",
    )
    sig = trace.output["x_signals"][0]
    assert sig["cross_consumer_ref"]["thematic_ledger_path"] is not None
    assert "100.yml" in sig["cross_consumer_ref"]["thematic_ledger_path"]


def test_compose_cross_consumer_ref_null_when_no_overlap(tmp_path):
    client = _StubClient(tweets_by_cashtag={"$NVDA": [_tweet(id="100")]})
    trace = compose(
        tickers=["NVDA"],
        client=client,
        classifier_callable=lambda t, c: _verdict(),
        snapshot_top_of_hour=TOP_OF_HOUR,
        thematic_x_root=tmp_path / "x",
        now_iso_fn=lambda: "2026-05-25T18:00:00+00:00",
    )
    sig = trace.output["x_signals"][0]
    assert sig["cross_consumer_ref"] == {"thematic_ledger_path": None}


def test_compose_emits_traceentry_for_ledger_embedding(tmp_path):
    client = _StubClient(tweets_by_cashtag={"$NVDA": []})
    trace = compose(
        tickers=["NVDA"],
        client=client,
        classifier_callable=lambda t, c: _verdict(),
        snapshot_top_of_hour=TOP_OF_HOUR,
        thematic_x_root=tmp_path / "x",
        now_iso_fn=lambda: "2026-05-25T18:00:00+00:00",
    )
    assert trace.tool == "tools/news_research/x_scanner.py"
    assert trace.inputs["classifier_provided"] is True
    assert "snapshot_top_of_hour" in trace.inputs


def test_compose_uses_top_of_hour_default_when_omitted(tmp_path):
    client = _StubClient(tweets_by_cashtag={"$NVDA": []})
    trace = compose(
        tickers=["NVDA"],
        client=client,
        classifier_callable=lambda t, c: _verdict(),
        thematic_x_root=tmp_path / "x",
    )
    # The default top-of-hour should be the current hour anchor
    iso = trace.output["snapshot_top_of_hour"]
    parsed = datetime.fromisoformat(iso)
    assert parsed.minute == 0 and parsed.second == 0


# ---------------------------------------------------------------------------
# Parse top-of-hour helper
# ---------------------------------------------------------------------------


def test_parse_top_of_hour_accepts_iso_string():
    dt = _parse_top_of_hour("2026-05-25T18:00:00+00:00")
    assert dt == TOP_OF_HOUR


def test_parse_top_of_hour_accepts_z_suffix():
    dt = _parse_top_of_hour("2026-05-25T18:00:00Z")
    assert dt == TOP_OF_HOUR


def test_parse_top_of_hour_accepts_datetime_passthrough():
    dt = _parse_top_of_hour(TOP_OF_HOUR)
    assert dt == TOP_OF_HOUR


def test_parse_top_of_hour_default_is_current_hour_anchor():
    dt = _parse_top_of_hour(None)
    now = datetime.now(timezone.utc)
    assert dt.minute == 0 and dt.second == 0
    assert abs((dt - now.replace(minute=0, second=0, microsecond=0)).total_seconds()) < 1
