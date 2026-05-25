"""Handle-driven X-timeline ingest for the thematic-portfolio corpus.

Polls a fixed Tier 1 / Tier 2 / Tier 3 account list (Aschenbrenner +
co-PMs + adjacent voices + ensemble fund principals) and writes per-post
YAML artifacts to ``ledgers/thematic/corpus/x/<YYYY-MM-DD>/<post_id>.yml``
per the v2 design at ``Bertieboo/wiki/notes/swing-thematic-portfolio-x-ingest-decision.md``.

## Scope (v1)

* **Ingester only.** Pulls tweets and writes the post artifact. The
  ``classifier_result`` / ``routing`` / ``drill_down`` blocks are written
  as null/empty stubs — the substantive-artifact classifier (downstream)
  reads-then-writes them.
* **No drill-down.** Reply + quote-chain expansion is deferred to v2 (only
  Tier-1-classifier-flagged posts qualify per the design).
* **No periodic profile refresh.** ``get_user_about`` is called once
  per account to resolve ``user_id``; profile metadata isn't tracked
  per-cycle.

## Polling cadence

| Tier | Accounts | Cadence (intended) |
|---|---|---|
| 1 | @leopoldasch + @CarlShulman | hourly |
| 2 | @philip_trammell + @AvitalBalwit + @sholtodouglas | 4-hourly |
| 3 | @bradgerstner + @plaffont + @TimWeiss_LSC | 4-hourly |

Cadence is enforced by the **caller** (Task Scheduler / cron). This
module exposes a ``--tier`` filter so the cron line can choose which
subset to poll on each fire. Running ``--tier 1`` hourly + ``--tier 2 3``
every 4 hours gives the documented schedule.

## State

Per-account ``last_seen_tweet_id`` lives at
``ledgers/thematic/corpus/x/_state/last_seen.json`` (gitignored). On
first ingest the field is absent — the ingester pulls the latest page
(20 tweets) and uses the most-recent tweet_id as the new high-water mark.

## Deduplication

Two layers:
* **State file** filters incremental polls (refuse to re-process tweets
  whose id is below ``last_seen_tweet_id``).
* **File existence check** guards against state-file corruption — if the
  per-post YAML already exists at the canonical path, the ingester skips
  it (writes the existing file as ``skipped`` in the per-cycle summary).

## Cost discipline

At the 7-account scope + designed cadence:
* ~2,520 polls/month × ~5 tweets/poll average = ~12,600 tweet returns/mo
* twitterapi.io bills per-tweet returned: 12,600 × $0.00015 = **~$1.90/mo**

Matches the design spec's $0.30-$1/mo estimate (which under-counted by
assuming most polls return zero new tweets — in practice the comparable-
window paginated response usually has 5+ tweets even when most are dupes).

## CLI

::

    # Poll Tier 1 only (typical hourly cron)
    uv run python -m tools.thematic_portfolio.corpus.x_ingest --tier 1

    # Poll all tiers (typical 4-hourly cron)
    uv run python -m tools.thematic_portfolio.corpus.x_ingest --tier 1 --tier 2 --tier 3

    # Dry-run: see what would fetch but don't write artifacts
    uv run python -m tools.thematic_portfolio.corpus.x_ingest --tier 1 --dry-run

    # Backfill from a specific tweet_id (overrides state file for that account)
    uv run python -m tools.thematic_portfolio.corpus.x_ingest \\
        --tier 1 --backfill-account leopoldasch --since-tweet-id 1234567890

## Output (per the design-spec schema)

::

    meta:
      schema_version: "1.0"
      source: twitterapi.io
      source_endpoint: get_user_last_tweets
      ingest_ts: 2026-05-25T18:30:00+00:00
      ingest_run_id: "x_ingest_<iso>_<account>"

    post:
      id: "..."
      author_username: "leopoldasch"
      author_id: "2989966781"
      author_tier: 1
      created_at: ...
      text: |
        ...
      in_reply_to_post_id: ...
      quote_post_id: ...
      ...

    engagement: { ... }
    classifier_result: null      # set by classifier later
    routing: null                # set by orchestrator later
    drill_down:                  # ingester writes empty struct
      fetched_quotes: false
      fetched_replies: false
      quote_ledger_paths: []
      reply_ledger_paths: []
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ...cli import emit
from ...contract import TraceEntry
from ...x_common.twitterapi_client import (
    TwitterAPIAuthError,
    TwitterAPIClient,
    TwitterAPIError,
)

TOOL = "tools/thematic_portfolio/corpus/x_ingest.py"
SCHEMA_VERSION = "1.0"
SOURCE_TAG = "twitterapi.io"
# Use advanced_search with from:<handle> rather than get_user_last_tweets —
# the latter empirically returns no rows for accounts with no recent activity
# (verified 2026-05-25 against @leopoldasch: last_tweets / tweet_timeline
# returned tweets=0 across 5 paginated calls; advanced_search returned 35
# tweets across 2 pages going back 19 months). Same cost-per-tweet; better
# coverage for the SA-LP-tier handles where activity is bursty.
SOURCE_ENDPOINT = "advanced_search"

DEFAULT_CORPUS_X_ROOT = Path("ledgers/thematic/corpus/x")
DEFAULT_STATE_PATH = DEFAULT_CORPUS_X_ROOT / "_state" / "last_seen.json"


# ---------------------------------------------------------------------------
# Account registry — locked per design spec (see vault notes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class XAccount:
    """One monitored X account."""

    username: str            # the @handle without the leading @
    tier: int                # 1 / 2 / 3 per the design spec

    def normalised_username(self) -> str:
        return self.username.lstrip("@").lower()


# Final list per the design spec (v1 — 8 accounts). Handles for Tier 3
# Coatue + Light Street are flagged in the design as "verification needed";
# v1 ships them and lets any 404 land as a per-account error. Better to
# fail loudly than silently skip.
ACCOUNTS: list[XAccount] = [
    # Tier 1 — primary signal, hourly
    XAccount(username="leopoldasch", tier=1),
    XAccount(username="CarlShulman", tier=1),
    # Tier 2 — secondary signal, 4-hourly
    XAccount(username="philip_trammell", tier=2),
    XAccount(username="AvitalBalwit", tier=2),
    XAccount(username="sholtodouglas", tier=2),
    # Tier 3 — ensemble fund principals, 4-hourly
    XAccount(username="bradgerstner", tier=3),
    XAccount(username="plaffont", tier=3),         # verify on first call
    XAccount(username="TimWeiss_LSC", tier=3),     # verify on first call
]


def accounts_for_tiers(tiers: Iterable[int]) -> list[XAccount]:
    """Filter the canonical ACCOUNTS list to the requested tier subset."""
    wanted = set(int(t) for t in tiers)
    return [a for a in ACCOUNTS if a.tier in wanted]


# ---------------------------------------------------------------------------
# State file (last-seen tweet_id per account)
# ---------------------------------------------------------------------------


def _load_state(state_path: Path) -> dict[str, dict[str, Any]]:
    """Load the state file; tolerates missing / corrupt."""
    if not state_path.exists():
        return {}
    try:
        doc = json.loads(state_path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(
    state_path: Path, state: dict[str, dict[str, Any]]
) -> None:
    """Write the state file with parent-dir creation."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tweet extraction — twitterapi.io response → design-spec YAML shape
# ---------------------------------------------------------------------------


def _author_username_from_tweet(tweet: dict[str, Any]) -> str | None:
    author = tweet.get("author") or {}
    return author.get("userName") or author.get("screen_name") or author.get("username")


def _author_id_from_tweet(tweet: dict[str, Any]) -> str | None:
    author = tweet.get("author") or {}
    raw_id = author.get("id") or author.get("user_id") or author.get("authorId")
    return str(raw_id) if raw_id is not None else None


def _extract_engagement(tweet: dict[str, Any], fetched_at_iso: str) -> dict[str, Any]:
    """Pull engagement counts from a twitterapi.io tweet dict."""
    return {
        "reply_count": int(tweet.get("replyCount") or 0),
        "retweet_count": int(tweet.get("retweetCount") or 0),
        "quote_count": int(tweet.get("quoteCount") or 0),
        "like_count": int(tweet.get("likeCount") or 0),
        "view_count": (
            int(tweet["viewCount"]) if tweet.get("viewCount") is not None else None
        ),
        "fetched_at": fetched_at_iso,
    }


def _extract_reply_quote_refs(tweet: dict[str, Any]) -> dict[str, Any]:
    """Pull reply + quote-tweet reference fields with tolerant key matching."""
    in_reply_to_post_id = (
        tweet.get("inReplyToId")
        or tweet.get("in_reply_to_status_id")
        or tweet.get("in_reply_to_post_id")
    )
    in_reply_to_author = (
        tweet.get("inReplyToUserName")
        or tweet.get("in_reply_to_screen_name")
        or tweet.get("in_reply_to_username")
    )
    quote_obj = tweet.get("quoted_tweet") or tweet.get("quotedTweet") or {}
    quote_post_id = (
        tweet.get("quoteId")
        or tweet.get("quotedStatusId")
        or quote_obj.get("id")
    )
    quote_post_text = quote_obj.get("text") if quote_obj else None
    return {
        "in_reply_to_post_id": str(in_reply_to_post_id) if in_reply_to_post_id else None,
        "in_reply_to_author": in_reply_to_author,
        "quote_post_id": str(quote_post_id) if quote_post_id else None,
        "quote_post_text": quote_post_text,
    }


def _extract_media_info(tweet: dict[str, Any]) -> tuple[bool, int]:
    """Detect attached media. Tolerant of multiple shapes."""
    entities = tweet.get("entities") or {}
    media = entities.get("media") or tweet.get("media") or tweet.get("mediaUrls")
    if media:
        count = len(media) if isinstance(media, list) else 1
        return True, count
    return False, 0


def _compose_post_artifact(
    *,
    tweet: dict[str, Any],
    account: XAccount,
    user_id_lookup: dict[str, str],
    fetched_at_iso: str,
    ingest_run_id: str,
) -> dict[str, Any]:
    """Compose one post-artifact dict matching the design-spec schema.

    ``user_id_lookup`` maps lowercase-username → user_id; falls back to the
    author block embedded in the tweet itself when missing.
    """
    post_id = str(tweet.get("id") or tweet.get("tweetId") or "").strip()
    if not post_id:
        raise ValueError(f"tweet missing id field: keys={list(tweet)[:8]}")

    tweet_author = _author_username_from_tweet(tweet) or account.username
    inline_author_id = _author_id_from_tweet(tweet)
    author_id = inline_author_id or user_id_lookup.get(account.normalised_username(), "")

    has_media, media_count = _extract_media_info(tweet)
    refs = _extract_reply_quote_refs(tweet)

    return {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "source": SOURCE_TAG,
            "source_endpoint": SOURCE_ENDPOINT,
            "ingest_ts": fetched_at_iso,
            "ingest_run_id": ingest_run_id,
        },
        "post": {
            "id": post_id,
            "author_username": tweet_author,
            "author_id": author_id,
            "author_tier": account.tier,
            "created_at": tweet.get("createdAt") or tweet.get("created_at"),
            "text": tweet.get("text") or "",
            "in_reply_to_post_id": refs["in_reply_to_post_id"],
            "in_reply_to_author": refs["in_reply_to_author"],
            "quote_post_id": refs["quote_post_id"],
            "quote_post_text": refs["quote_post_text"],
            "has_media": has_media,
            "media_count": media_count,
            "language": tweet.get("lang") or tweet.get("language") or "en",
            "url": tweet.get("url") or f"https://x.com/{tweet_author}/status/{post_id}",
        },
        "engagement": _extract_engagement(tweet, fetched_at_iso),
        "classifier_result": None,
        "routing": None,
        "drill_down": {
            "fetched_quotes": False,
            "fetched_replies": False,
            "quote_ledger_paths": [],
            "reply_ledger_paths": [],
        },
    }


# ---------------------------------------------------------------------------
# YAML writer (stdlib only — minimal subset)
# ---------------------------------------------------------------------------


_YAML_NULL_STRINGS = frozenset({"null", "Null", "NULL", "~", ""})
_YAML_BOOL_STRINGS = frozenset({
    "true", "True", "TRUE", "false", "False", "FALSE",
    "yes", "Yes", "YES", "no", "No", "NO",
    "on", "On", "ON", "off", "Off", "OFF",
})


def _looks_like_yaml_scalar_non_string(s: str) -> bool:
    """Return True if ``s`` unquoted would be parsed as a non-string scalar.

    Conservative — anything PyYAML would coerce to int / float / bool /
    null on safe_load needs to be force-quoted so we preserve the string
    type round-trip.
    """
    if s in _YAML_NULL_STRINGS:
        return True
    if s in _YAML_BOOL_STRINGS:
        return True
    # Integer literal (optionally signed, no leading zeros except "0" itself)
    if s.lstrip("-+").isdigit():
        return True
    # Float literal — try parsing; ValueError = not a float
    try:
        float(s)
        return True
    except ValueError:
        return False


def _to_yaml(obj: Any, indent: int = 0) -> str:
    """Hand-rolled YAML emitter for the post-artifact dict shape.

    Sufficient for the documented schema; doesn't try to be a full YAML
    library. Strings get the literal-block scalar ``|`` form when they
    contain newlines.
    """
    pad = "  " * indent
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, (int, float)):
        return repr(obj)
    if isinstance(obj, str):
        if "\n" in obj:
            # Literal block scalar with strip indicator |- so PyYAML
            # round-trips back to the same string without a trailing newline.
            return "|-\n" + "\n".join(pad + "  " + line for line in obj.splitlines())
        # Inline string — quote when it would otherwise be parsed as a
        # non-string (numeric, bool, null) or when it contains YAML
        # control characters.
        needs_quoting = (
            _looks_like_yaml_scalar_non_string(obj)
            or any(c in obj for c in ":#&*!|>%@`")
            or obj.startswith(("-", "?", " "))
            or obj == ""
        )
        if needs_quoting:
            escaped = obj.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return obj
    if isinstance(obj, list):
        if not obj:
            return "[]"
        out_lines: list[str] = []
        for item in obj:
            rendered = _to_yaml(item, indent + 1)
            if isinstance(item, (dict, list)) and rendered != "{}" and rendered != "[]":
                out_lines.append(f"{pad}-")
                out_lines.append(_indent_block(rendered, indent + 1))
            else:
                out_lines.append(f"{pad}- {rendered}")
        return "\n".join(out_lines)
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        out_lines = []
        for k, v in obj.items():
            rendered = _to_yaml(v, indent + 1)
            if isinstance(v, (dict, list)) and rendered not in ("{}", "[]"):
                out_lines.append(f"{pad}{k}:")
                out_lines.append(_indent_block(rendered, indent + 1))
            else:
                out_lines.append(f"{pad}{k}: {rendered}")
        return "\n".join(out_lines)
    raise TypeError(f"unsupported YAML type: {type(obj).__name__}")


def _indent_block(block: str, indent: int) -> str:
    """Re-indent a pre-rendered YAML block to a deeper level."""
    pad = "  " * indent
    return "\n".join((pad + line if line and not line.startswith(pad) else line)
                     for line in block.splitlines())


def _artifact_path(corpus_x_root: Path, post: dict[str, Any]) -> Path:
    """Compose the canonical per-post YAML path."""
    created = post["post"].get("created_at") or ""
    # Best-effort YYYY-MM-DD extraction. twitterapi.io uses several formats;
    # we slice the first 10 chars when they look like an ISO date, else
    # fall back to today's date.
    if isinstance(created, str) and len(created) >= 10 and created[4] == "-" and created[7] == "-":
        date_str = created[:10]
    else:
        # Try to parse "Wed Mar 06 18:20:51 +0000 2024" style
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(created)
            date_str = dt.strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return corpus_x_root / date_str / f"{post['post']['id']}.yml"


# ---------------------------------------------------------------------------
# Compose loop
# ---------------------------------------------------------------------------


@dataclass
class AccountResult:
    """Outcome of one account's polling cycle."""

    account: XAccount
    fetched: int = 0
    new_artifacts: int = 0
    skipped_existing: int = 0
    skipped_below_high_water: int = 0
    high_water_before: str | None = None
    high_water_after: str | None = None
    error: str | None = None
    paths_written: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account.username,
            "tier": self.account.tier,
            "fetched": self.fetched,
            "new_artifacts": self.new_artifacts,
            "skipped_existing": self.skipped_existing,
            "skipped_below_high_water": self.skipped_below_high_water,
            "high_water_before": self.high_water_before,
            "high_water_after": self.high_water_after,
            "error": self.error,
            "paths_written": self.paths_written,
        }


def _resolve_user_id(
    client: TwitterAPIClient,
    account: XAccount,
    state: dict[str, dict[str, Any]],
) -> str | None:
    """Resolve user_id for one account.

    Caches the resolved id in the per-account state block so we don't
    re-resolve on every cycle. ``user_id`` is documented as "more stable
    and faster than userName" — worth the one-time lookup.
    """
    key = account.normalised_username()
    cached = state.get(key, {}).get("user_id")
    if cached:
        return cached
    resp = client.get_user_by_username(account.username)
    data = resp.body.get("data") or {}
    uid = data.get("id")
    if uid is None:
        return None
    state.setdefault(key, {})["user_id"] = str(uid)
    state[key]["resolved_at"] = state.get(key, {}).get("resolved_at") or _utc_now_iso()
    return str(uid)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def poll_account(
    *,
    client: TwitterAPIClient,
    account: XAccount,
    state: dict[str, dict[str, Any]],
    corpus_x_root: Path,
    fetched_at_iso: str,
    ingest_run_id: str,
    max_pages: int = 5,
    backfill_since_tweet_id: str | None = None,
    dry_run: bool = False,
) -> AccountResult:
    """Poll one account: fetch new tweets, write YAML artifacts, update state."""
    result = AccountResult(account=account)
    try:
        uid = _resolve_user_id(client, account, state)
        if uid is None:
            result.error = "could_not_resolve_user_id"
            return result

        # ``filter_water_mark`` is the cut-off used during the loop —
        # backfill can lower this to ingest older tweets. ``state_water_mark``
        # is the existing canonical high-water in the state file — backfill
        # must never undo forward progress on this value.
        state_water_mark = state.get(
            account.normalised_username(), {}
        ).get("last_seen_tweet_id")
        filter_water_mark = backfill_since_tweet_id or state_water_mark
        result.high_water_before = filter_water_mark
        new_high_water = state_water_mark
        user_id_lookup = {account.normalised_username(): uid}

        # advanced_search is more reliable than last_tweets for low-activity
        # accounts. `from:<handle>` is the X advanced-search operator that
        # filters to one author. `-is:retweet` matches the documented swing
        # x_scanner convention (replies stay in — substantive thesis-updates
        # sometimes route through reply chains per the design spec § Edge cases).
        query = f"from:{account.username} -is:retweet"
        for tweet in client.iter_advanced_search(
            query=query, query_type="Latest", max_pages=max_pages,
        ):
            result.fetched += 1
            tweet_id = str(tweet.get("id") or tweet.get("tweetId") or "")
            if not tweet_id:
                continue

            if filter_water_mark and tweet_id <= filter_water_mark:
                # Tweet IDs are snowflake-monotonic; lexicographic compare
                # works because they're same-length numeric strings.
                # iter_* generator yields in newest-first order, so a hit
                # against the water-mark means everything beyond is old too.
                result.skipped_below_high_water += 1
                break

            artifact = _compose_post_artifact(
                tweet=tweet,
                account=account,
                user_id_lookup=user_id_lookup,
                fetched_at_iso=fetched_at_iso,
                ingest_run_id=ingest_run_id,
            )
            out_path = _artifact_path(corpus_x_root, artifact)

            if out_path.exists():
                result.skipped_existing += 1
            elif dry_run:
                result.paths_written.append(str(out_path))
                result.new_artifacts += 1
            else:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(_to_yaml(artifact), encoding="utf-8")
                result.paths_written.append(str(out_path))
                result.new_artifacts += 1

            # Update high-water by max() so backfill never undoes forward
            # progress on the existing state water mark.
            if new_high_water is None or tweet_id > new_high_water:
                new_high_water = tweet_id

        if new_high_water and new_high_water != state_water_mark:
            state.setdefault(account.normalised_username(), {})[
                "last_seen_tweet_id"
            ] = new_high_water
            state[account.normalised_username()]["last_polled_at"] = fetched_at_iso
            result.high_water_after = new_high_water
        else:
            result.high_water_after = state_water_mark
    except TwitterAPIAuthError as e:
        result.error = f"auth: {e}"
    except TwitterAPIError as e:
        result.error = f"api: {e}"
    except Exception as e:  # noqa: BLE001 — last-resort per-account isolation
        result.error = f"{type(e).__name__}: {e}"
    return result


def compose(
    *,
    tiers: list[int],
    client: TwitterAPIClient | None = None,
    corpus_x_root: Path = DEFAULT_CORPUS_X_ROOT,
    state_path: Path = DEFAULT_STATE_PATH,
    max_pages_per_account: int = 5,
    backfill_account: str | None = None,
    backfill_since_tweet_id: str | None = None,
    dry_run: bool = False,
    now_iso_fn: Callable[[], str] = _utc_now_iso,
) -> TraceEntry:
    """Run one polling cycle.

    Args:
        tiers: list of tier numbers to poll (1 / 2 / 3). Empty = no work.
        client: TwitterAPIClient — created on demand when omitted.
        corpus_x_root: artifact directory root.
        state_path: per-account state file.
        max_pages_per_account: pagination cap.
        backfill_account: when set, ignores state file high-water-mark for
            this account only; uses ``backfill_since_tweet_id`` instead.
        backfill_since_tweet_id: see above.
        dry_run: when True, no YAML files are written + state isn't saved.
        now_iso_fn: clock injection for tests.

    Returns:
        TraceEntry whose ``output`` summarizes the cycle.
    """
    fetched_at_iso = now_iso_fn()
    ingest_run_id_template = f"x_ingest_{fetched_at_iso.replace(':', '_')}_{{account}}"

    client = client or TwitterAPIClient()
    state = _load_state(state_path)

    accounts = accounts_for_tiers(tiers)
    results: list[AccountResult] = []

    for account in accounts:
        backfill_id = (
            backfill_since_tweet_id
            if backfill_account
            and backfill_account.lstrip("@").lower() == account.normalised_username()
            else None
        )
        result = poll_account(
            client=client,
            account=account,
            state=state,
            corpus_x_root=corpus_x_root,
            fetched_at_iso=fetched_at_iso,
            ingest_run_id=ingest_run_id_template.format(account=account.username),
            max_pages=max_pages_per_account,
            backfill_since_tweet_id=backfill_id,
            dry_run=dry_run,
        )
        results.append(result)

    if not dry_run:
        _save_state(state_path, state)

    summary = {
        "fetched_at": fetched_at_iso,
        "tiers": sorted(set(tiers)),
        "n_accounts_polled": len(results),
        "n_new_artifacts": sum(r.new_artifacts for r in results),
        "n_skipped_existing": sum(r.skipped_existing for r in results),
        "n_errors": sum(1 for r in results if r.error),
        "dry_run": dry_run,
        "per_account": [r.to_dict() for r in results],
    }

    return TraceEntry(
        tool=TOOL,
        inputs={
            "tiers": sorted(set(tiers)),
            "max_pages_per_account": max_pages_per_account,
            "dry_run": dry_run,
            "backfill_account": backfill_account,
            "backfill_since_tweet_id": backfill_since_tweet_id,
        },
        output=summary,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.corpus.x_ingest",
        description=__doc__,
    )
    p.add_argument(
        "--tier", type=int, choices=[1, 2, 3], action="append", required=True,
        help="Which tier(s) to poll. Pass multiple times for multi-tier polls.",
    )
    p.add_argument(
        "--corpus-x-root",
        default=str(DEFAULT_CORPUS_X_ROOT),
        help=f"Artifact root (default: {DEFAULT_CORPUS_X_ROOT}).",
    )
    p.add_argument(
        "--state-path",
        default=str(DEFAULT_STATE_PATH),
        help=f"State file path (default: {DEFAULT_STATE_PATH}).",
    )
    p.add_argument(
        "--max-pages-per-account", type=int, default=5,
        help="Pagination cap per account. Default 5 (= 100 tweets max).",
    )
    p.add_argument(
        "--backfill-account",
        help="When backfilling, the @handle (without @) to override state for.",
    )
    p.add_argument(
        "--since-tweet-id",
        help="Tweet-id high-water mark for the backfill account.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Compose + emit summary; skip artifact + state writes.",
    )
    return p


def main() -> int:
    args = _build_arg_parser().parse_args()
    if args.backfill_account and not args.since_tweet_id:
        print(
            "error: --backfill-account requires --since-tweet-id", file=sys.stderr,
        )
        return 2
    trace = compose(
        tiers=list(args.tier),
        corpus_x_root=Path(args.corpus_x_root),
        state_path=Path(args.state_path),
        max_pages_per_account=args.max_pages_per_account,
        backfill_account=args.backfill_account,
        backfill_since_tweet_id=args.since_tweet_id,
        dry_run=args.dry_run,
    )
    emit(trace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
