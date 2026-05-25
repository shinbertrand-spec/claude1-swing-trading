"""Tests for tools.thematic_portfolio.corpus.x_ingest."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.thematic_portfolio.corpus.x_ingest import (
    ACCOUNTS,
    DEFAULT_CORPUS_X_ROOT,
    DEFAULT_STATE_PATH,
    SCHEMA_VERSION,
    XAccount,
    _artifact_path,
    _compose_post_artifact,
    _extract_engagement,
    _extract_media_info,
    _extract_reply_quote_refs,
    _load_state,
    _save_state,
    _to_yaml,
    accounts_for_tiers,
    compose,
    poll_account,
)


# ---------------------------------------------------------------------------
# Stub TwitterAPIClient
# ---------------------------------------------------------------------------


class _StubClient:
    """Drop-in replacement for TwitterAPIClient.

    Constructed with ``profiles`` (username → user_id mapping) and
    ``tweets_by_username`` (lowercase username → list of tweet dicts,
    newest-first). x_ingest uses ``iter_advanced_search`` with a
    ``from:<handle>`` query, so the stub keys tweets by handle.

    Raises a configured exception when the username appears in
    ``profile_errors`` or ``tweet_errors``.
    """

    def __init__(
        self,
        *,
        profiles: dict[str, str],
        tweets_by_username: dict[str, list[dict]] | None = None,
        profile_errors: dict[str, Exception] | None = None,
        tweet_errors: dict[str, Exception] | None = None,
    ):
        self._profiles = profiles
        self._tweets_by_username = tweets_by_username or {}
        self._profile_errors = profile_errors or {}
        self._tweet_errors = tweet_errors or {}
        self.profile_calls: list[str] = []
        self.search_calls: list[str] = []

    def get_user_by_username(self, username):
        self.profile_calls.append(username)
        if username in self._profile_errors:
            raise self._profile_errors[username]
        uid = self._profiles.get(username.lstrip("@").lower())
        if uid is None:
            return _StubResp({"status": "success", "data": {}})
        return _StubResp({"status": "success", "data": {"id": uid}})

    def iter_advanced_search(self, *, query, query_type="Latest", max_pages=10):
        self.search_calls.append(query)
        # x_ingest builds queries like ``from:leopoldasch -is:retweet``.
        # Extract the handle to look up the stubbed tweet list.
        handle = ""
        for tok in query.split():
            if tok.lower().startswith("from:"):
                handle = tok.split(":", 1)[1].lstrip("@").lower()
                break
        if handle in self._tweet_errors:
            raise self._tweet_errors[handle]
        yield from self._tweets_by_username.get(handle, [])


class _StubResp:
    def __init__(self, body):
        self.body = body


# ---------------------------------------------------------------------------
# Tweet fixtures
# ---------------------------------------------------------------------------


def _tweet(
    *,
    id="100",
    text="Hello world",
    created_at="2026-05-25T18:30:00+00:00",
    author_username="leopoldasch",
    author_id="2989966781",
    in_reply_to=None,
    quote_id=None,
    likes=100,
    retweets=20,
    replies=5,
    views=10000,
):
    t: dict = {
        "id": id,
        "text": text,
        "createdAt": created_at,
        "author": {"userName": author_username, "id": author_id},
        "likeCount": likes,
        "retweetCount": retweets,
        "replyCount": replies,
        "viewCount": views,
    }
    if in_reply_to:
        t["inReplyToId"] = in_reply_to["post_id"]
        t["inReplyToUserName"] = in_reply_to["author"]
    if quote_id:
        t["quoted_tweet"] = {"id": quote_id["id"], "text": quote_id["text"]}
    return t


# ---------------------------------------------------------------------------
# Account registry discipline
# ---------------------------------------------------------------------------


def test_accounts_contain_all_three_tiers():
    tiers = {a.tier for a in ACCOUNTS}
    assert tiers == {1, 2, 3}


def test_accounts_tier1_contains_leopold_and_shulman():
    tier1 = {a.username for a in ACCOUNTS if a.tier == 1}
    assert tier1 == {"leopoldasch", "CarlShulman"}


def test_accounts_no_duplicates_in_canonical_list():
    handles = [a.normalised_username() for a in ACCOUNTS]
    assert len(handles) == len(set(handles))


def test_accounts_for_tiers_filters_correctly():
    only_tier1 = accounts_for_tiers([1])
    assert {a.username for a in only_tier1} == {"leopoldasch", "CarlShulman"}
    only_23 = accounts_for_tiers([2, 3])
    assert all(a.tier in (2, 3) for a in only_23)
    assert len(only_23) > 0


def test_accounts_for_tiers_dedups_inputs():
    """Passing duplicate tiers shouldn't multiply accounts in the result."""
    assert len(accounts_for_tiers([1, 1, 1])) == 2


def test_xaccount_normalises_at_prefix_and_case():
    acc = XAccount(username="@LeopoldAsch", tier=1)
    assert acc.normalised_username() == "leopoldasch"


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------


def test_load_state_returns_empty_dict_when_missing(tmp_path):
    assert _load_state(tmp_path / "nope.json") == {}


def test_load_state_returns_empty_dict_on_corruption(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("not json {{{")
    assert _load_state(p) == {}


def test_save_then_load_round_trip(tmp_path):
    p = tmp_path / "sub" / "state.json"
    payload = {"leopoldasch": {"user_id": "123", "last_seen_tweet_id": "abc"}}
    _save_state(p, payload)
    assert _load_state(p) == payload


# ---------------------------------------------------------------------------
# Tweet extractors
# ---------------------------------------------------------------------------


def test_extract_engagement_pulls_all_counts():
    e = _extract_engagement(
        _tweet(likes=100, retweets=20, replies=5, views=10000),
        fetched_at_iso="2026-05-25T18:30:00+00:00",
    )
    assert e == {
        "reply_count": 5,
        "retweet_count": 20,
        "quote_count": 0,
        "like_count": 100,
        "view_count": 10000,
        "fetched_at": "2026-05-25T18:30:00+00:00",
    }


def test_extract_engagement_view_count_null_when_missing():
    t = _tweet()
    del t["viewCount"]
    e = _extract_engagement(t, "2026-05-25T18:30:00+00:00")
    assert e["view_count"] is None


def test_extract_reply_quote_refs_handles_reply_only():
    t = _tweet(in_reply_to={"post_id": "999", "author": "someoneelse"})
    refs = _extract_reply_quote_refs(t)
    assert refs["in_reply_to_post_id"] == "999"
    assert refs["in_reply_to_author"] == "someoneelse"
    assert refs["quote_post_id"] is None
    assert refs["quote_post_text"] is None


def test_extract_reply_quote_refs_handles_quote_only():
    t = _tweet(quote_id={"id": "555", "text": "the quoted body"})
    refs = _extract_reply_quote_refs(t)
    assert refs["quote_post_id"] == "555"
    assert refs["quote_post_text"] == "the quoted body"
    assert refs["in_reply_to_post_id"] is None


def test_extract_reply_quote_refs_handles_neither():
    refs = _extract_reply_quote_refs(_tweet())
    assert refs == {
        "in_reply_to_post_id": None,
        "in_reply_to_author": None,
        "quote_post_id": None,
        "quote_post_text": None,
    }


def test_extract_media_info_detects_media():
    t = _tweet()
    t["entities"] = {"media": [{"type": "photo"}, {"type": "video"}]}
    has_media, count = _extract_media_info(t)
    assert has_media is True
    assert count == 2


def test_extract_media_info_no_media():
    has_media, count = _extract_media_info(_tweet())
    assert has_media is False
    assert count == 0


# ---------------------------------------------------------------------------
# _compose_post_artifact + _artifact_path
# ---------------------------------------------------------------------------


def test_compose_post_artifact_emits_all_schema_blocks():
    acc = XAccount(username="leopoldasch", tier=1)
    artifact = _compose_post_artifact(
        tweet=_tweet(id="100"),
        account=acc,
        user_id_lookup={"leopoldasch": "2989966781"},
        fetched_at_iso="2026-05-25T18:30:00+00:00",
        ingest_run_id="x_ingest_run42",
    )
    # All five top-level blocks per the design-spec schema
    assert set(artifact.keys()) == {
        "meta", "post", "engagement", "classifier_result",
        "routing", "drill_down",
    }
    assert artifact["meta"]["schema_version"] == SCHEMA_VERSION
    assert artifact["meta"]["source"] == "twitterapi.io"
    assert artifact["post"]["author_tier"] == 1
    assert artifact["post"]["id"] == "100"
    assert artifact["classifier_result"] is None
    assert artifact["routing"] is None
    assert artifact["drill_down"] == {
        "fetched_quotes": False,
        "fetched_replies": False,
        "quote_ledger_paths": [],
        "reply_ledger_paths": [],
    }


def test_compose_post_artifact_raises_on_missing_id():
    acc = XAccount(username="leopoldasch", tier=1)
    t = _tweet()
    del t["id"]
    with pytest.raises(ValueError):
        _compose_post_artifact(
            tweet=t,
            account=acc,
            user_id_lookup={},
            fetched_at_iso="2026-05-25T18:30:00+00:00",
            ingest_run_id="run",
        )


def test_compose_post_artifact_synthesises_url_when_missing():
    acc = XAccount(username="leopoldasch", tier=1)
    artifact = _compose_post_artifact(
        tweet=_tweet(id="100"),
        account=acc,
        user_id_lookup={},
        fetched_at_iso="2026-05-25T18:30:00+00:00",
        ingest_run_id="run",
    )
    assert artifact["post"]["url"] == "https://x.com/leopoldasch/status/100"


def test_artifact_path_uses_iso_date_from_created_at(tmp_path):
    acc = XAccount(username="leopoldasch", tier=1)
    artifact = _compose_post_artifact(
        tweet=_tweet(id="100", created_at="2026-05-25T18:30:00+00:00"),
        account=acc, user_id_lookup={}, fetched_at_iso="x", ingest_run_id="r",
    )
    p = _artifact_path(tmp_path / "x", artifact)
    assert p == tmp_path / "x" / "2026-05-25" / "100.yml"


def test_artifact_path_falls_back_to_today_when_created_at_malformed(tmp_path):
    acc = XAccount(username="leopoldasch", tier=1)
    artifact = _compose_post_artifact(
        tweet=_tweet(id="100", created_at=""),
        account=acc, user_id_lookup={}, fetched_at_iso="x", ingest_run_id="r",
    )
    p = _artifact_path(tmp_path / "x", artifact)
    # Date dir should be a YYYY-MM-DD slug (10 chars)
    assert len(p.parent.name) == 10
    assert p.parent.name[4] == "-" and p.parent.name[7] == "-"


# ---------------------------------------------------------------------------
# _to_yaml round-trip
# ---------------------------------------------------------------------------


def test_to_yaml_round_trips_via_safe_load():
    """The hand-rolled YAML emitter must produce output that PyYAML (the
    standard parser) reads back as the same dict shape."""
    yaml = pytest.importorskip("yaml")
    sample = {
        "meta": {"schema_version": "1.0", "ingest_ts": "2026-05-25T18:30:00+00:00"},
        "post": {
            "id": "100",
            "text": "Hello\nWorld",
            "in_reply_to_post_id": None,
            "has_media": True,
        },
        "engagement": {"like_count": 100, "view_count": None},
        "drill_down": {
            "quote_ledger_paths": [],
            "fetched_replies": False,
        },
        "classifier_result": None,
    }
    rendered = _to_yaml(sample)
    parsed = yaml.safe_load(rendered)
    assert parsed == sample


def test_to_yaml_emits_empty_list_inline():
    assert _to_yaml({"items": []}).endswith("items: []")


def test_to_yaml_emits_null_literal():
    rendered = _to_yaml({"classifier_result": None})
    assert "classifier_result: null" in rendered


# ---------------------------------------------------------------------------
# poll_account
# ---------------------------------------------------------------------------


def test_poll_account_writes_yaml_for_new_tweets(tmp_path):
    acc = XAccount(username="leopoldasch", tier=1)
    client = _StubClient(
        profiles={"leopoldasch": "2989966781"},
        tweets_by_username={"leopoldasch": [
            _tweet(id="200", text="newer"),
            _tweet(id="100", text="older"),
        ]},
    )
    state: dict = {}
    result = poll_account(
        client=client,
        account=acc,
        state=state,
        corpus_x_root=tmp_path / "x",
        fetched_at_iso="2026-05-25T18:30:00+00:00",
        ingest_run_id="r",
    )
    assert result.error is None
    assert result.fetched == 2
    assert result.new_artifacts == 2
    # state file updated with both user_id + highest seen tweet
    assert state["leopoldasch"]["user_id"] == "2989966781"
    assert state["leopoldasch"]["last_seen_tweet_id"] == "200"
    # YAML files written
    assert (tmp_path / "x" / "2026-05-25" / "200.yml").exists()
    assert (tmp_path / "x" / "2026-05-25" / "100.yml").exists()


def test_poll_account_respects_state_high_water(tmp_path):
    acc = XAccount(username="leopoldasch", tier=1)
    client = _StubClient(
        profiles={"leopoldasch": "2989966781"},
        tweets_by_username={"leopoldasch": [
            _tweet(id="300", text="newest"),
            _tweet(id="200", text="prior high water"),
            _tweet(id="100", text="should-be-skipped"),
        ]},
    )
    state = {"leopoldasch": {"user_id": "2989966781", "last_seen_tweet_id": "200"}}
    result = poll_account(
        client=client,
        account=acc,
        state=state,
        corpus_x_root=tmp_path / "x",
        fetched_at_iso="2026-05-25T18:30:00+00:00",
        ingest_run_id="r",
    )
    assert result.new_artifacts == 1
    assert result.skipped_below_high_water == 1
    assert state["leopoldasch"]["last_seen_tweet_id"] == "300"
    assert (tmp_path / "x" / "2026-05-25" / "300.yml").exists()
    assert not (tmp_path / "x" / "2026-05-25" / "100.yml").exists()


def test_poll_account_skips_existing_files(tmp_path):
    """Defense-in-depth dedup — even if state file is wiped, existing YAML
    files prevent re-writes."""
    acc = XAccount(username="leopoldasch", tier=1)
    out_dir = tmp_path / "x" / "2026-05-25"
    out_dir.mkdir(parents=True)
    (out_dir / "200.yml").write_text("pre-existing")
    client = _StubClient(
        profiles={"leopoldasch": "2989966781"},
        tweets_by_username={"leopoldasch": [_tweet(id="200")]},
    )
    state: dict = {}
    result = poll_account(
        client=client,
        account=acc,
        state=state,
        corpus_x_root=tmp_path / "x",
        fetched_at_iso="2026-05-25T18:30:00+00:00",
        ingest_run_id="r",
    )
    assert result.skipped_existing == 1
    assert result.new_artifacts == 0
    # Existing content untouched
    assert (out_dir / "200.yml").read_text() == "pre-existing"


def test_poll_account_dry_run_writes_nothing(tmp_path):
    acc = XAccount(username="leopoldasch", tier=1)
    client = _StubClient(
        profiles={"leopoldasch": "2989966781"},
        tweets_by_username={"leopoldasch": [_tweet(id="200")]},
    )
    state: dict = {}
    result = poll_account(
        client=client,
        account=acc,
        state=state,
        corpus_x_root=tmp_path / "x",
        fetched_at_iso="2026-05-25T18:30:00+00:00",
        ingest_run_id="r",
        dry_run=True,
    )
    assert result.new_artifacts == 1
    assert "200.yml" in result.paths_written[0]
    assert not (tmp_path / "x" / "2026-05-25" / "200.yml").exists()


def test_poll_account_caches_user_id_after_first_resolution(tmp_path):
    acc = XAccount(username="leopoldasch", tier=1)
    client = _StubClient(
        profiles={"leopoldasch": "2989966781"},
        tweets_by_username={"leopoldasch": []},
    )
    state: dict = {}
    poll_account(
        client=client, account=acc, state=state,
        corpus_x_root=tmp_path / "x", fetched_at_iso="x", ingest_run_id="r",
    )
    assert client.profile_calls == ["leopoldasch"]
    # Second poll — should NOT call get_user_by_username again
    poll_account(
        client=client, account=acc, state=state,
        corpus_x_root=tmp_path / "x", fetched_at_iso="y", ingest_run_id="r2",
    )
    assert client.profile_calls == ["leopoldasch"]


def test_poll_account_records_api_error_per_account(tmp_path):
    from tools.x_common.twitterapi_client import TwitterAPIError
    acc = XAccount(username="leopoldasch", tier=1)
    client = _StubClient(
        profiles={"leopoldasch": "2989966781"},
        tweets_by_username={"leopoldasch": []},
        tweet_errors={"leopoldasch": TwitterAPIError("503 server")},
    )
    state: dict = {}
    result = poll_account(
        client=client, account=acc, state=state,
        corpus_x_root=tmp_path / "x", fetched_at_iso="x", ingest_run_id="r",
    )
    assert result.error is not None
    assert "api:" in result.error
    assert result.new_artifacts == 0


def test_poll_account_records_auth_error(tmp_path):
    from tools.x_common.twitterapi_client import TwitterAPIAuthError
    acc = XAccount(username="leopoldasch", tier=1)
    client = _StubClient(
        profiles={},
        profile_errors={"leopoldasch": TwitterAPIAuthError("401")},
    )
    state: dict = {}
    result = poll_account(
        client=client, account=acc, state=state,
        corpus_x_root=tmp_path / "x", fetched_at_iso="x", ingest_run_id="r",
    )
    assert result.error is not None
    assert "auth:" in result.error


def test_poll_account_handles_unknown_user(tmp_path):
    """get_user_by_username returns empty data block — record + skip."""
    acc = XAccount(username="nonexistent_handle", tier=3)
    client = _StubClient(profiles={})  # empty → returns {"data": {}}
    result = poll_account(
        client=client, account=acc, state={},
        corpus_x_root=tmp_path / "x", fetched_at_iso="x", ingest_run_id="r",
    )
    assert result.error == "could_not_resolve_user_id"


def test_poll_account_backfill_does_not_undo_higher_state_water_mark(tmp_path):
    """Backfill lowers the LOOP filter (so older tweets can be ingested)
    but MUST NOT clobber the canonical state water-mark if it's already
    higher. Otherwise an operator using --backfill to fetch a few missed
    tweets would silently lose forward progress."""
    acc = XAccount(username="leopoldasch", tier=1)
    client = _StubClient(
        profiles={"leopoldasch": "2989966781"},
        tweets_by_username={"leopoldasch": [
            _tweet(id="500"),
            _tweet(id="300"),  # below backfill mark
            _tweet(id="100"),
        ]},
    )
    state = {"leopoldasch": {"user_id": "2989966781", "last_seen_tweet_id": "999999"}}
    result = poll_account(
        client=client, account=acc, state=state,
        corpus_x_root=tmp_path / "x",
        fetched_at_iso="2026-05-25T18:30:00+00:00",
        ingest_run_id="r",
        backfill_since_tweet_id="400",
    )
    # 500 > 400 (backfill cutoff) → written; 300 ≤ 400 → loop stops
    assert result.new_artifacts == 1
    assert result.skipped_below_high_water == 1
    # State water-mark UNCHANGED — 500 < 999999 so no forward progress.
    assert state["leopoldasch"]["last_seen_tweet_id"] == "999999"


def test_poll_account_backfill_can_lower_loop_filter_without_lowering_state(tmp_path):
    """Backfill with NO existing state water mark sets state to the max
    ingested tweet — normal forward-progress semantics."""
    acc = XAccount(username="leopoldasch", tier=1)
    client = _StubClient(
        profiles={"leopoldasch": "2989966781"},
        tweets_by_username={"leopoldasch": [_tweet(id="500"), _tweet(id="300")]},
    )
    state = {"leopoldasch": {"user_id": "2989966781"}}  # no last_seen yet
    result = poll_account(
        client=client, account=acc, state=state,
        corpus_x_root=tmp_path / "x",
        fetched_at_iso="2026-05-25T18:30:00+00:00",
        ingest_run_id="r",
        backfill_since_tweet_id="200",
    )
    assert result.new_artifacts == 2  # 500 + 300 both > 200
    assert state["leopoldasch"]["last_seen_tweet_id"] == "500"


# ---------------------------------------------------------------------------
# compose — top-level cycle
# ---------------------------------------------------------------------------


def test_compose_polls_only_requested_tier(tmp_path, monkeypatch):
    client = _StubClient(
        profiles={
            "leopoldasch": "1", "carlshulman": "2",
            "philip_trammell": "3", "avitalbalwit": "4", "sholtodouglas": "5",
            "bradgerstner": "6", "plaffont": "7", "timweiss_lsc": "8",
        },
        tweets_by_username={},
    )
    trace = compose(
        tiers=[1],
        client=client,
        corpus_x_root=tmp_path / "x",
        state_path=tmp_path / "state.json",
    )
    out = trace.output
    assert out["n_accounts_polled"] == 2  # Tier 1 has exactly 2 accounts
    polled_handles = {r["account"] for r in out["per_account"]}
    assert polled_handles == {"leopoldasch", "CarlShulman"}


def test_compose_dry_run_does_not_persist_state(tmp_path):
    client = _StubClient(
        profiles={"leopoldasch": "1", "carlshulman": "2"},
        tweets_by_username={"leopoldasch": [_tweet(id="100")]},
    )
    state_path = tmp_path / "state.json"
    compose(
        tiers=[1],
        client=client,
        corpus_x_root=tmp_path / "x",
        state_path=state_path,
        dry_run=True,
    )
    assert not state_path.exists()


def test_compose_persists_state_on_real_run(tmp_path):
    client = _StubClient(
        profiles={"leopoldasch": "1", "carlshulman": "2"},
        tweets_by_username={"leopoldasch": [_tweet(id="100", author_username="leopoldasch")]},
    )
    state_path = tmp_path / "state.json"
    compose(
        tiers=[1],
        client=client,
        corpus_x_root=tmp_path / "x",
        state_path=state_path,
    )
    assert state_path.exists()
    saved = json.loads(state_path.read_text())
    assert saved["leopoldasch"]["user_id"] == "1"
    assert saved["leopoldasch"]["last_seen_tweet_id"] == "100"


def test_compose_aggregate_counts_match_per_account(tmp_path):
    client = _StubClient(
        profiles={"leopoldasch": "1", "carlshulman": "2"},
        tweets_by_username={
            "leopoldasch": [_tweet(id="100", author_username="leopoldasch")],
            "carlshulman": [_tweet(id="200", author_username="CarlShulman")],
        },
    )
    trace = compose(
        tiers=[1],
        client=client,
        corpus_x_root=tmp_path / "x",
        state_path=tmp_path / "state.json",
        now_iso_fn=lambda: "2026-05-25T18:30:00+00:00",
    )
    out = trace.output
    assert out["n_new_artifacts"] == 2
    assert out["n_errors"] == 0
    assert out["fetched_at"] == "2026-05-25T18:30:00+00:00"


def test_compose_emits_traceentry_for_ledger_embedding(tmp_path):
    client = _StubClient(profiles={"leopoldasch": "1", "carlshulman": "2"})
    trace = compose(
        tiers=[1],
        client=client,
        corpus_x_root=tmp_path / "x",
        state_path=tmp_path / "state.json",
    )
    assert trace.tool == "tools/thematic_portfolio/corpus/x_ingest.py"
    assert trace.fetched_at
    assert "tiers" in trace.inputs


def test_default_paths_are_under_thematic_corpus():
    assert DEFAULT_CORPUS_X_ROOT == Path("ledgers/thematic/corpus/x")
    assert DEFAULT_STATE_PATH.name == "last_seen.json"
    assert "_state" in DEFAULT_STATE_PATH.parts
