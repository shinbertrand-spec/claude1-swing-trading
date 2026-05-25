"""Tests for tools.x_common.twitterapi_client."""
from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from tools.x_common.twitterapi_client import (
    API_KEY_ENV_VAR,
    APIResponse,
    DEFAULT_ENV_PATH,
    TwitterAPIAuthError,
    TwitterAPIClient,
    TwitterAPIError,
    TwitterAPIRateLimitError,
    _read_api_key_from_env_file,
    resolve_api_key,
)


# ---------------------------------------------------------------------------
# Stub HTTP infrastructure
# ---------------------------------------------------------------------------


class _StubHTTPResponse:
    """Minimal urllib-like response object."""

    def __init__(self, body: dict | str, status: int = 200):
        if isinstance(body, dict):
            raw = json.dumps(body).encode("utf-8")
        else:
            raw = body.encode("utf-8") if isinstance(body, str) else body
        self._buf = io.BytesIO(raw)
        self.status = status

    def read(self):
        return self._buf.read()

    def getcode(self):
        return self.status


def _make_opener(*, responses: list, history: list | None = None):
    """Build a stub-opener that returns the next response on each call.

    Items in ``responses`` can be:
    * ``dict`` — interpreted as a 200 JSON body.
    * ``_StubHTTPResponse`` — returned directly.
    * ``urllib.error.HTTPError`` — raised on the call.
    * ``urllib.error.URLError`` — raised on the call.
    * ``Exception`` instances — raised on the call.
    """
    idx = {"i": 0}
    history = history if history is not None else []

    def opener(req, timeout=None):
        i = idx["i"]
        idx["i"] += 1
        if i >= len(responses):
            raise AssertionError(f"unexpected extra request #{i}: {req.full_url}")
        history.append(req.full_url)
        item = responses[i]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, _StubHTTPResponse):
            return item
        if isinstance(item, dict):
            return _StubHTTPResponse(item)
        raise AssertionError(f"unsupported stub item: {item!r}")

    return opener


def _client(*, responses, api_key: str = "test-key", history=None, **kwargs):
    """Convenience: build a TwitterAPIClient wired to a stub opener.

    Sleep is no-op'd so retry tests don't actually wait.
    """
    opener = _make_opener(responses=responses, history=history)
    return TwitterAPIClient(
        api_key=api_key,
        http_opener=opener,
        sleep_fn=lambda _s: None,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


def test_resolve_api_key_prefers_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv(API_KEY_ENV_VAR, "from-env")
    env = tmp_path / ".env"
    env.write_text(f"{API_KEY_ENV_VAR}=from-file")
    assert resolve_api_key(explicit="explicit-key", env_path=env) == "explicit-key"


def test_resolve_api_key_uses_env_var_when_no_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv(API_KEY_ENV_VAR, "from-env")
    env = tmp_path / ".env"
    env.write_text(f"{API_KEY_ENV_VAR}=from-file")
    assert resolve_api_key(explicit=None, env_path=env) == "from-env"


def test_resolve_api_key_falls_back_to_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"{API_KEY_ENV_VAR}=from-file")
    assert resolve_api_key(explicit=None, env_path=env) == "from-file"


def test_resolve_api_key_returns_none_when_nothing_set(monkeypatch, tmp_path):
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    assert resolve_api_key(explicit=None, env_path=tmp_path / "missing.env") is None


def test_read_env_file_handles_comments_and_quotes(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment line\n"
        "\n"
        f"{API_KEY_ENV_VAR}='quoted-value'\n"
        "OTHER_VAR=ignored\n"
    )
    assert _read_api_key_from_env_file(env) == "quoted-value"


def test_read_env_file_returns_none_when_file_missing(tmp_path):
    assert _read_api_key_from_env_file(tmp_path / "nope.env") is None


def test_default_env_path_points_at_claude_channels_directory():
    assert DEFAULT_ENV_PATH == Path.home() / ".claude" / "channels" / "twitterapi" / ".env"


# ---------------------------------------------------------------------------
# _request — auth + retry behavior
# ---------------------------------------------------------------------------


def test_request_raises_auth_error_when_no_credential(monkeypatch, tmp_path):
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    client = TwitterAPIClient(
        api_key=None,
        env_path=tmp_path / "missing.env",
        http_opener=_make_opener(responses=[]),
        sleep_fn=lambda _s: None,
    )
    with pytest.raises(TwitterAPIAuthError):
        client.get_user_by_username("leopoldasch")


def test_request_sends_api_key_header(monkeypatch):
    captured_headers: dict = {}

    def opener(req, timeout=None):
        for k, v in req.header_items():
            captured_headers[k] = v
        return _StubHTTPResponse({"status": "success", "data": {}})

    client = TwitterAPIClient(api_key="my-secret-key", http_opener=opener)
    client.get_user_by_username("leopoldasch")
    # urllib normalizes header names — accept either capitalization
    found = {k.lower(): v for k, v in captured_headers.items()}
    assert found.get("x-api-key") == "my-secret-key"


def test_request_returns_parsed_body_and_status():
    body = {
        "tweets": [{"id": "1", "text": "hello"}],
        "has_next_page": False,
        "next_cursor": "",
        "status": "success",
    }
    client = _client(responses=[body])
    resp = client.get_user_last_tweets(user_name="leopoldasch")
    assert isinstance(resp, APIResponse)
    assert resp.body == body
    assert resp.http_status == 200
    assert resp.content_length_bytes > 0
    assert resp.endpoint_path == "/twitter/user/last_tweets"
    assert resp.requested_at_iso.endswith("+00:00")


def test_request_retries_on_429_then_succeeds():
    err_429 = urllib.error.HTTPError(
        url="x", code=429, msg="rate", hdrs=None, fp=io.BytesIO(b"slow down"),
    )
    success = {"status": "success", "data": {"id": "1"}}
    history: list[str] = []
    client = _client(responses=[err_429, success], history=history)
    resp = client.get_user_by_username("leopoldasch")
    assert resp.status_ok
    assert len(history) == 2


def test_request_raises_ratelimit_after_retries_exhausted():
    err_429 = urllib.error.HTTPError(
        url="x", code=429, msg="rate", hdrs=None, fp=io.BytesIO(b"again"),
    )
    client = _client(responses=[err_429, err_429, err_429])
    with pytest.raises(TwitterAPIRateLimitError):
        client.get_user_by_username("leopoldasch")


def test_request_raises_auth_error_on_401():
    err_401 = urllib.error.HTTPError(
        url="x", code=401, msg="unauthorized",
        hdrs=None, fp=io.BytesIO(b'{"status":"error"}'),
    )
    client = _client(responses=[err_401])
    with pytest.raises(TwitterAPIAuthError):
        client.get_user_by_username("leopoldasch")


def test_request_raises_auth_error_on_403():
    err_403 = urllib.error.HTTPError(
        url="x", code=403, msg="forbidden",
        hdrs=None, fp=io.BytesIO(b"forbidden"),
    )
    client = _client(responses=[err_403])
    with pytest.raises(TwitterAPIAuthError):
        client.get_user_by_username("leopoldasch")


def test_request_raises_error_on_500():
    err_500 = urllib.error.HTTPError(
        url="x", code=500, msg="server",
        hdrs=None, fp=io.BytesIO(b"oops"),
    )
    client = _client(responses=[err_500])
    with pytest.raises(TwitterAPIError) as exc_info:
        client.get_user_by_username("leopoldasch")
    assert not isinstance(exc_info.value, (TwitterAPIAuthError, TwitterAPIRateLimitError))


def test_request_retries_on_network_error_then_succeeds():
    net_err = urllib.error.URLError("connection reset")
    success = {"status": "success", "data": {}}
    history: list[str] = []
    client = _client(responses=[net_err, success], history=history)
    client.get_user_by_username("leopoldasch")
    assert len(history) == 2


def test_request_raises_error_on_invalid_json():
    bad_resp = _StubHTTPResponse(body="not-json-at-all", status=200)
    client = _client(responses=[bad_resp])
    with pytest.raises(TwitterAPIError):
        client.get_user_by_username("leopoldasch")


# ---------------------------------------------------------------------------
# URL building — endpoint paths + parameter shapes
# ---------------------------------------------------------------------------


def test_get_user_by_username_hits_correct_endpoint():
    history: list[str] = []
    client = _client(
        responses=[{"status": "success", "data": {"id": "12345"}}],
        history=history,
    )
    client.get_user_by_username("leopoldasch")
    assert "/twitter/user/info" in history[0]
    assert "userName=leopoldasch" in history[0]


def test_get_user_last_tweets_requires_username_or_userid():
    client = _client(responses=[])
    with pytest.raises(ValueError):
        client.get_user_last_tweets()


def test_get_user_last_tweets_passes_cursor_and_include_replies():
    history: list[str] = []
    client = _client(
        responses=[{"tweets": [], "has_next_page": False, "next_cursor": ""}],
        history=history,
    )
    client.get_user_last_tweets(
        user_name="leopoldasch", cursor="abc123", include_replies=True,
    )
    url = history[0]
    assert "/twitter/user/last_tweets" in url
    assert "userName=leopoldasch" in url
    assert "cursor=abc123" in url
    assert "includeReplies=true" in url


def test_get_user_timeline_uses_userid_param():
    history: list[str] = []
    client = _client(
        responses=[{"tweets": [], "has_next_page": False, "next_cursor": ""}],
        history=history,
    )
    client.get_user_timeline(user_id="98765432101")
    assert "/twitter/user/tweet_timeline" in history[0]
    assert "userId=98765432101" in history[0]


def test_advanced_search_passes_query_and_query_type():
    history: list[str] = []
    client = _client(
        responses=[{"tweets": [], "has_next_page": False, "next_cursor": ""}],
        history=history,
    )
    client.advanced_search(query="$NVDA -is:retweet lang:en")
    url = history[0]
    assert "/twitter/tweet/advanced_search" in url
    # urllib URL-encodes the query string — assert the encoded form.
    assert "queryType=Latest" in url
    # $ encodes as %24; space as +; : as %3A
    assert "%24NVDA" in url


def test_get_tweet_replies_uses_v2_path():
    history: list[str] = []
    client = _client(
        responses=[{"replies": [], "has_next_page": False, "next_cursor": ""}],
        history=history,
    )
    client.get_tweet_replies(tweet_id="1234567890")
    assert "/twitter/tweet/replies/v2" in history[0]
    assert "tweetId=1234567890" in history[0]


def test_get_tweet_quotes_hits_quotes_endpoint():
    history: list[str] = []
    client = _client(
        responses=[{"tweets": [], "has_next_page": False, "next_cursor": ""}],
        history=history,
    )
    client.get_tweet_quotes(tweet_id="1234567890", since_time=1700000000)
    url = history[0]
    assert "/twitter/tweet/quotes" in url
    assert "sinceTime=1700000000" in url


def test_get_user_about_hits_user_about_endpoint():
    history: list[str] = []
    client = _client(
        responses=[{"status": "success", "data": {"about_profile": {}}}],
        history=history,
    )
    client.get_user_about("leopoldasch")
    assert "/twitter/user_about" in history[0]
    assert "userName=leopoldasch" in history[0]


def test_build_url_drops_none_params_and_serialises_booleans():
    history: list[str] = []
    client = _client(
        responses=[{"tweets": [], "has_next_page": False, "next_cursor": ""}],
        history=history,
    )
    # since_time=None should NOT appear in the URL; includeReplies=False should
    # appear as "false" (not "False", not "0").
    client.get_tweet_quotes(
        tweet_id="x", since_time=None, until_time=None, include_replies=False,
    )
    url = history[0]
    assert "sinceTime" not in url
    assert "untilTime" not in url
    assert "includeReplies=false" in url


# ---------------------------------------------------------------------------
# APIResponse.next_cursor + status_ok
# ---------------------------------------------------------------------------


def test_response_next_cursor_when_has_next_page_true():
    resp = APIResponse(
        body={"has_next_page": True, "next_cursor": "abc"},
        http_status=200,
        content_length_bytes=10,
        endpoint_path="/x",
        requested_at_iso="2026-05-25T00:00:00+00:00",
    )
    assert resp.next_cursor == "abc"


def test_response_next_cursor_none_when_has_next_page_false():
    resp = APIResponse(
        body={"has_next_page": False, "next_cursor": "abc"},
        http_status=200,
        content_length_bytes=10,
        endpoint_path="/x",
        requested_at_iso="2026-05-25T00:00:00+00:00",
    )
    assert resp.next_cursor is None


def test_response_next_cursor_none_when_cursor_empty():
    resp = APIResponse(
        body={"has_next_page": True, "next_cursor": ""},
        http_status=200,
        content_length_bytes=10,
        endpoint_path="/x",
        requested_at_iso="2026-05-25T00:00:00+00:00",
    )
    assert resp.next_cursor is None


def test_response_status_ok_when_success():
    resp = APIResponse(
        body={"status": "success"},
        http_status=200,
        content_length_bytes=5,
        endpoint_path="/x",
        requested_at_iso="2026-05-25T00:00:00+00:00",
    )
    assert resp.status_ok


def test_response_status_ok_false_when_error():
    resp = APIResponse(
        body={"status": "error", "message": "oops"},
        http_status=200,
        content_length_bytes=5,
        endpoint_path="/x",
        requested_at_iso="2026-05-25T00:00:00+00:00",
    )
    assert not resp.status_ok


# ---------------------------------------------------------------------------
# iter_* generators — pagination + max_pages cap
# ---------------------------------------------------------------------------


def test_iter_user_last_tweets_paginates_until_exhausted():
    page1 = {
        "tweets": [{"id": "1"}, {"id": "2"}],
        "has_next_page": True,
        "next_cursor": "cursor-2",
    }
    page2 = {
        "tweets": [{"id": "3"}],
        "has_next_page": False,
        "next_cursor": "",
    }
    client = _client(responses=[page1, page2])
    tweets = list(client.iter_user_last_tweets(user_name="leopoldasch"))
    assert [t["id"] for t in tweets] == ["1", "2", "3"]


def test_iter_user_last_tweets_caps_at_max_pages():
    """max_pages prevents runaway pagination if has_next_page never flips."""
    page = {
        "tweets": [{"id": "x"}],
        "has_next_page": True,
        "next_cursor": "endless",
    }
    # Provide 100 stubbed responses; max_pages=3 should consume exactly 3.
    client = _client(responses=[page] * 100)
    tweets = list(
        client.iter_user_last_tweets(user_name="leopoldasch", max_pages=3)
    )
    assert len(tweets) == 3


def test_iter_advanced_search_paginates_until_exhausted():
    page1 = {
        "tweets": [{"id": "a"}, {"id": "b"}],
        "has_next_page": True,
        "next_cursor": "next",
    }
    page2 = {
        "tweets": [{"id": "c"}],
        "has_next_page": False,
        "next_cursor": "",
    }
    client = _client(responses=[page1, page2])
    tweets = list(client.iter_advanced_search(query="$NVDA"))
    assert [t["id"] for t in tweets] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Backoff cadence
# ---------------------------------------------------------------------------


def test_backoff_doubles_between_retries():
    """Exponential backoff: attempt 1 fails → sleep 1s, attempt 2 fails →
    sleep 2s, attempt 3 succeeds. We verify the sleep durations."""
    err = urllib.error.HTTPError(
        url="x", code=429, msg="rate", hdrs=None, fp=io.BytesIO(b""),
    )
    success = {"status": "success", "data": {}}
    sleeps: list[float] = []
    opener = _make_opener(responses=[err, err, success])
    client = TwitterAPIClient(
        api_key="k",
        http_opener=opener,
        sleep_fn=lambda s: sleeps.append(s),
        backoff_base=1.0,
    )
    client.get_user_by_username("x")
    assert sleeps == [1.0, 2.0]
