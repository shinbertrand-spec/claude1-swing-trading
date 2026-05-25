"""twitterapi.io HTTP client — shared by both X consumers.

Wraps the 7 twitterapi.io endpoints that the thematic-portfolio
``x_ingest`` and news-research ``x_scanner`` need:

============================  ============================================
Method                        Endpoint
============================  ============================================
``get_user_by_username``      ``GET /twitter/user/info``
``get_user_last_tweets``      ``GET /twitter/user/last_tweets``
``get_user_timeline``         ``GET /twitter/user/tweet_timeline``
``advanced_search``           ``GET /twitter/tweet/advanced_search``
``get_tweet_replies``         ``GET /twitter/tweet/replies/v2``
``get_tweet_quotes``          ``GET /twitter/tweet/quotes``
``get_user_about``            ``GET /twitter/user_about``
============================  ============================================

Base URL: ``https://api.twitterapi.io``. Auth: ``X-API-Key`` header.

All endpoints return 20 items per page wrapped in
``{tweets|replies|data: [...], has_next_page: bool, next_cursor: str,
status: "success"|"error", message: str}``. The client returns the raw
response dict — pagination + dedup is the caller's concern (different
consumers have different dedup keys).

Auth resolution chain (matches the
``tools/thematic_portfolio/kill_switch/telegram_alert.py`` pattern):

1. Explicit ``api_key=`` constructor argument
2. ``TWITTERAPI_IO_API_KEY`` environment variable
3. ``TWITTERAPI_IO_API_KEY=`` line in ``~/.claude/channels/twitterapi/.env``

If none resolve, :class:`TwitterAPIClient` raises
:class:`TwitterAPIAuthError` on the first request.

## Errors

* :class:`TwitterAPIAuthError` — credential missing OR 401 / 403 from server.
* :class:`TwitterAPIRateLimitError` — 429 after retries exhausted. The
  client retries automatically with exponential backoff (3 attempts by
  default); only the final failure raises.
* :class:`TwitterAPIError` — any other 4xx / 5xx or malformed JSON.

Network timeouts surface as :class:`TwitterAPIError` with the underlying
URLError message preserved.

## Cost discipline

Per the design specs, expected combined monthly spend is $0.30 (thematic)
+ $2.30-3.50 (swing) + $0.60-1.20 (classifier) = ~$3-6/month at scoped
volume. This client doesn't enforce a budget — that's the consumer's job.
But it DOES return the request body size so the caller can track spend
(twitterapi.io bills per-tweet returned, not per-request).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.twitterapi.io"
DEFAULT_ENV_PATH = Path.home() / ".claude" / "channels" / "twitterapi" / ".env"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 1.0  # seconds — exponential backoff base
API_KEY_HEADER = "X-API-Key"
API_KEY_ENV_VAR = "TWITTERAPI_IO_API_KEY"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TwitterAPIError(Exception):
    """Base class — any non-recoverable error talking to twitterapi.io."""


class TwitterAPIAuthError(TwitterAPIError):
    """Credential missing OR 401 / 403 returned by the server."""


class TwitterAPIRateLimitError(TwitterAPIError):
    """429 returned after retries were exhausted."""


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


def _read_api_key_from_env_file(env_path: Path) -> str | None:
    """Parse a shell-style ``.env`` file and return the API key, or None."""
    if not env_path.exists():
        return None
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == API_KEY_ENV_VAR:
                return value.strip().strip("'\"")
    except OSError:
        return None
    return None


def resolve_api_key(
    *,
    explicit: str | None = None,
    env_var: str = API_KEY_ENV_VAR,
    env_path: Path = DEFAULT_ENV_PATH,
) -> str | None:
    """Resolve the twitterapi.io API key via the documented chain.

    Returns None when no source yields a key — the caller decides whether
    to raise (the client raises lazily on first request).
    """
    if explicit:
        return explicit
    from_env = os.environ.get(env_var)
    if from_env:
        return from_env
    return _read_api_key_from_env_file(env_path)


# ---------------------------------------------------------------------------
# Response wrapper
# ---------------------------------------------------------------------------


@dataclass
class APIResponse:
    """Lightweight wrapper around a twitterapi.io JSON response.

    ``body`` is the parsed JSON. ``http_status`` is the HTTP status code.
    ``content_length_bytes`` is the raw response body size — useful for
    cost-tracking spot checks (twitterapi.io bills per-tweet returned but
    the response body size is a reasonable upper-bound proxy).

    The consumer pulls ``tweets`` / ``replies`` / ``data`` keys out of
    ``body`` itself; this wrapper is intentionally thin.
    """

    body: dict[str, Any]
    http_status: int
    content_length_bytes: int
    endpoint_path: str
    requested_at_iso: str

    @property
    def status_ok(self) -> bool:
        return self.body.get("status") in (None, "success")

    @property
    def next_cursor(self) -> str | None:
        """Pagination cursor for the next page, or None when exhausted.

        twitterapi.io's ``has_next_page == False`` is the canonical "stop"
        signal; the ``next_cursor`` field may still be present but should
        not be used.
        """
        if not self.body.get("has_next_page"):
            return None
        cursor = self.body.get("next_cursor")
        return cursor if cursor else None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class TwitterAPIClient:
    """Synchronous twitterapi.io client.

    Constructed without arguments resolves the API key via
    :func:`resolve_api_key`. Pass ``api_key=`` for tests or one-off scripts.

    Pass ``http_opener=`` to inject a stub for tests — anything matching
    the ``urllib.request.OpenerDirector.open`` signature works.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        env_path: Path = DEFAULT_ENV_PATH,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        http_opener=None,
        clock_fn=None,
        sleep_fn=None,
    ) -> None:
        self._api_key: str | None = resolve_api_key(
            explicit=api_key, env_path=env_path
        )
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_retries = max(1, int(max_retries))
        self._backoff_base = backoff_base
        self._http_opener = http_opener
        # DI for tests; defaults to time.time / time.sleep
        self._clock_fn = clock_fn or (lambda: time.time())
        self._sleep_fn = sleep_fn or (lambda s: time.sleep(s))

    # ----------------------------------- low-level

    def _build_url(self, path: str, params: dict[str, Any]) -> str:
        # Only include non-None params; encode booleans as "true"/"false"
        # to match twitterapi.io's documented query-string convention.
        encoded: list[tuple[str, str]] = []
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, bool):
                encoded.append((k, "true" if v else "false"))
            else:
                encoded.append((k, str(v)))
        query = urllib.parse.urlencode(encoded)
        url = f"{self._base_url}{path}"
        return f"{url}?{query}" if query else url

    def _request(self, path: str, params: dict[str, Any] | None = None) -> APIResponse:
        if not self._api_key:
            raise TwitterAPIAuthError(
                f"twitterapi.io API key unresolved — set {API_KEY_ENV_VAR} env var "
                f"or create {DEFAULT_ENV_PATH}"
            )
        params = params or {}
        url = self._build_url(path, params)
        requested_at_iso = _now_iso(self._clock_fn)

        last_err: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                req = urllib.request.Request(
                    url,
                    method="GET",
                    headers={
                        API_KEY_HEADER: self._api_key,
                        "Accept": "application/json",
                        "User-Agent": "Claude1-x-common/1.0",
                    },
                )
                if self._http_opener is not None:
                    resp = self._http_opener(req, timeout=self._timeout)
                else:
                    resp = urllib.request.urlopen(req, timeout=self._timeout)
                raw = resp.read()
                status_code = getattr(resp, "status", None) or resp.getcode()
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise TwitterAPIError(
                        f"non-JSON response from {path}: {exc}"
                    ) from exc
                return APIResponse(
                    body=body,
                    http_status=int(status_code or 200),
                    content_length_bytes=len(raw),
                    endpoint_path=path,
                    requested_at_iso=requested_at_iso,
                )
            except urllib.error.HTTPError as e:
                # Read body once — server error messages are sometimes JSON.
                try:
                    err_body = e.read().decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    err_body = ""
                if e.code in (401, 403):
                    raise TwitterAPIAuthError(
                        f"{e.code} from {path}: {err_body}"
                    ) from e
                if e.code == 429 and attempt < self._max_retries:
                    # Exponential backoff: 1s, 2s, 4s, ...
                    delay = self._backoff_base * (2 ** (attempt - 1))
                    self._sleep_fn(delay)
                    last_err = e
                    continue
                if e.code == 429:
                    raise TwitterAPIRateLimitError(
                        f"429 from {path} after {attempt} attempts: {err_body}"
                    ) from e
                raise TwitterAPIError(
                    f"HTTP {e.code} from {path}: {err_body}"
                ) from e
            except urllib.error.URLError as e:
                # Transient network errors get the same retry treatment as 429.
                if attempt < self._max_retries:
                    delay = self._backoff_base * (2 ** (attempt - 1))
                    self._sleep_fn(delay)
                    last_err = e
                    continue
                raise TwitterAPIError(
                    f"network error talking to {path}: {e.reason}"
                ) from e
        # Should be unreachable — loop body either returns or raises.
        raise TwitterAPIError(  # pragma: no cover
            f"unreachable: retry loop exited without return for {path}: {last_err}"
        )

    # ----------------------------------- public endpoints

    def get_user_by_username(self, username: str) -> APIResponse:
        """``GET /twitter/user/info?userName=<username>``."""
        return self._request("/twitter/user/info", {"userName": username})

    def get_user_last_tweets(
        self,
        *,
        user_name: str | None = None,
        user_id: str | None = None,
        cursor: str | None = None,
        include_replies: bool = False,
    ) -> APIResponse:
        """``GET /twitter/user/last_tweets``.

        Pass exactly one of ``user_name`` or ``user_id``. ``user_id`` is
        recommended per the docs ("could be more stable and faster than
        userName"). First page uses ``cursor=""`` (or None).
        """
        if not user_name and not user_id:
            raise ValueError("get_user_last_tweets requires user_name or user_id")
        return self._request(
            "/twitter/user/last_tweets",
            {
                "userName": user_name,
                "userId": user_id,
                "cursor": cursor if cursor is not None else "",
                "includeReplies": include_replies,
            },
        )

    def get_user_timeline(
        self,
        *,
        user_id: str,
        cursor: str | None = None,
        include_replies: bool = False,
        include_parent_tweet: bool = False,
    ) -> APIResponse:
        """``GET /twitter/user/tweet_timeline?userId=...``."""
        return self._request(
            "/twitter/user/tweet_timeline",
            {
                "userId": user_id,
                "cursor": cursor if cursor is not None else "",
                "includeReplies": include_replies,
                "includeParentTweet": include_parent_tweet,
            },
        )

    def advanced_search(
        self,
        *,
        query: str,
        query_type: str = "Latest",
        cursor: str | None = None,
    ) -> APIResponse:
        """``GET /twitter/tweet/advanced_search?query=...``.

        ``query`` supports Twitter advanced-search syntax — cashtags like
        ``$NVDA``, operators like ``-is:retweet lang:en``, etc.
        """
        return self._request(
            "/twitter/tweet/advanced_search",
            {
                "query": query,
                "queryType": query_type,
                "cursor": cursor if cursor is not None else "",
            },
        )

    def get_tweet_replies(
        self,
        *,
        tweet_id: str,
        cursor: str | None = None,
        query_type: str = "Relevance",
    ) -> APIResponse:
        """``GET /twitter/tweet/replies/v2?tweetId=...``."""
        return self._request(
            "/twitter/tweet/replies/v2",
            {
                "tweetId": tweet_id,
                "cursor": cursor if cursor is not None else "",
                "queryType": query_type,
            },
        )

    def get_tweet_quotes(
        self,
        *,
        tweet_id: str,
        cursor: str | None = None,
        since_time: int | None = None,
        until_time: int | None = None,
        include_replies: bool = True,
    ) -> APIResponse:
        """``GET /twitter/tweet/quotes?tweetId=...``."""
        return self._request(
            "/twitter/tweet/quotes",
            {
                "tweetId": tweet_id,
                "cursor": cursor if cursor is not None else "",
                "sinceTime": since_time,
                "untilTime": until_time,
                "includeReplies": include_replies,
            },
        )

    def get_user_about(self, username: str) -> APIResponse:
        """``GET /twitter/user_about?userName=<username>``."""
        return self._request("/twitter/user_about", {"userName": username})

    # ----------------------------------- iteration helpers

    def iter_user_last_tweets(
        self,
        *,
        user_name: str | None = None,
        user_id: str | None = None,
        include_replies: bool = False,
        max_pages: int = 50,
    ):
        """Generator over paginated ``get_user_last_tweets``.

        Yields raw tweet dicts. Stops on ``has_next_page=False`` or after
        ``max_pages`` (whichever comes first). ``max_pages`` is a safety
        cap — at 20 tweets/page, 50 pages = 1000 tweets, well over a
        typical hourly poll's needs.
        """
        cursor: str | None = ""
        for _ in range(max_pages):
            resp = self.get_user_last_tweets(
                user_name=user_name,
                user_id=user_id,
                cursor=cursor,
                include_replies=include_replies,
            )
            tweets = resp.body.get("tweets") or []
            for t in tweets:
                yield t
            cursor = resp.next_cursor
            if cursor is None:
                return

    def iter_advanced_search(
        self,
        *,
        query: str,
        query_type: str = "Latest",
        max_pages: int = 10,
    ):
        """Generator over paginated ``advanced_search``.

        Default ``max_pages=10`` (200 tweets) is appropriate for swing
        cashtag-per-hour scans where Stage 1 filters discard most posts.
        """
        cursor: str | None = ""
        for _ in range(max_pages):
            resp = self.advanced_search(
                query=query, query_type=query_type, cursor=cursor,
            )
            tweets = resp.body.get("tweets") or []
            for t in tweets:
                yield t
            cursor = resp.next_cursor
            if cursor is None:
                return


# ---------------------------------------------------------------------------
# Module-level convenience helpers
# ---------------------------------------------------------------------------


def _now_iso(clock_fn) -> str:
    """ISO-8601 UTC timestamp from injected clock — supports float seconds
    OR a callable returning an ISO string directly (test convenience)."""
    val = clock_fn()
    if isinstance(val, str):
        return val
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(val), tz=timezone.utc).isoformat(
        timespec="seconds"
    )
