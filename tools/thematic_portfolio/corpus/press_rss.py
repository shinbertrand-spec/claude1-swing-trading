"""Press-feed RSS fetcher — pulls Tier-1 outlet RSS feeds, filters for
Aschenbrenner / SA LP / Shulman / Trammell mentions, writes markdown
artifacts into the corpus directory.

This is the v1 press fetcher. Scope:

* Two outlets to start: Fortune + Semafor (the two that have written
  about SA LP per the existing seed corpus).
* Per match, save: title + RSS-feed description (~200-500 chars usually)
  + URL + pubDate. NO WebFetch of the full article body — most Tier-1
  outlets are paywalled (WSJ, FT, NYT, Bloomberg, The Information) and
  Loop 1 can WebFetch itself if it decides it needs more.
* Output: markdown file at
  ``ledgers/thematic/corpus/press/<date>-<source>-<slug>.md``.
* Idempotency: scan the output directory for existing frontmatter
  ``url:`` fields; skip any match already on disk.

Filter keywords (case-insensitive, matched against title OR description):

* "Aschenbrenner"
* "Situational Awareness" (the fund AND the essay)
* "Carl Shulman"
* "Philip Trammell"

These are deliberately conservative. "SA LP" alone is too short to
disambiguate. The Loop 1 reasoning layer can prioritise via tags +
fired_at later.

## CLI

::

    uv run python -m tools.thematic_portfolio.corpus.press_rss
    uv run python -m tools.thematic_portfolio.corpus.press_rss --outlets fortune
    uv run python -m tools.thematic_portfolio.corpus.press_rss --since 2026-05-01
    uv run python -m tools.thematic_portfolio.corpus.press_rss --dry-run

## Library

::

    from tools.thematic_portfolio.corpus.press_rss import fetch_and_save
    result = fetch_and_save(outlets=["fortune"], output_dir=Path(...))
    print(result.output)  # TraceEntry

## Adding outlets

Append to :data:`OUTLET_CATALOG` with the RSS URL + a short source label.
Most Tier-1 outlets that publish RSS use RSS 2.0; Atom would need
parser extension (deferred — Fortune and Semafor are both RSS 2.0).
"""
from __future__ import annotations

import argparse
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional

from ...cli import emit
from ...contract import TraceEntry

TOOL = "tools/thematic_portfolio/corpus/press_rss.py"

DEFAULT_OUTPUT_DIR = Path("ledgers/thematic/corpus/press")
DEFAULT_FETCH_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class OutletConfig:
    """One press outlet's RSS configuration."""

    key: str             # e.g. "fortune" — CLI selector
    label: str           # human-readable, used in frontmatter tags
    rss_url: str
    slug_prefix: str     # included in output filename — "fortune", "semafor"


# Catalog of supported outlets. Each entry MUST be a publicly-accessible
# RSS or Atom feed (no auth flow). Verified 2026-05-26:
#   - Fortune (200, RSS 2.0, ~10 most-recent business articles per fetch)
#   - FT Companies (200, RSS 2.0, ~25 most-recent companies coverage;
#     paywalled body but headlines + RSS summaries are accessible — useful
#     for capturing SA LP / hedge-fund mentions in the FT companies stream)
#   - Hacker News front-page RSS (200, RSS 2.0; SA LP / Aschenbrenner
#     essays + 13F coverage occasionally surface in top stories)
# Deferred: Semafor (no public RSS — all probed URLs 404 incl. /feed,
# /rss, /feed.xml, /technology/rss), WSJ (auth), Bloomberg (no public
# RSS), Reuters (Cloudflare-blocked on default UA).
OUTLET_CATALOG: dict[str, OutletConfig] = {
    "fortune": OutletConfig(
        key="fortune",
        label="Fortune",
        rss_url="https://fortune.com/feed/",
        slug_prefix="fortune",
    ),
    "ft_companies": OutletConfig(
        key="ft_companies",
        label="FT Companies",
        rss_url="https://www.ft.com/companies?format=rss",
        slug_prefix="ft",
    ),
    "hacker_news": OutletConfig(
        key="hacker_news",
        label="Hacker News",
        rss_url="https://news.ycombinator.com/rss",
        slug_prefix="hn",
    ),
}


# Filter keywords. Case-insensitive substring match against title OR description.
# Conservative — "SA LP" alone would false-positive on unrelated content.
FILTER_KEYWORDS: tuple[str, ...] = (
    "Aschenbrenner",
    "Situational Awareness",
    "Carl Shulman",
    "Philip Trammell",
)


@dataclass
class PressItem:
    """One RSS item that passed the filter."""

    title: str
    description: str
    url: str
    pub_date_iso: Optional[str]
    outlet_key: str
    outlet_label: str
    matched_keywords: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FetchAndSaveResult:
    """Composite result for one CLI / library invocation."""

    n_outlets_polled: int
    n_items_matched: int
    n_items_written: int
    n_items_skipped_duplicate: int
    n_items_skipped_old: int
    n_outlets_errored: int
    written_paths: list[str]
    errors: list[dict[str, str]]
    items: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


_SLUG_BAD_CHARS_RE = re.compile(r"[^a-z0-9-]+")
_MULTI_DASH_RE = re.compile(r"-+")


def _slugify(title: str, max_len: int = 60) -> str:
    """Convert a title to a kebab-case slug suitable for filenames."""
    s = title.lower()
    s = _SLUG_BAD_CHARS_RE.sub("-", s)
    s = _MULTI_DASH_RE.sub("-", s).strip("-")
    if len(s) > max_len:
        # Truncate at last dash before max_len so we don't cut a word
        cut = s[:max_len].rfind("-")
        s = s[:cut] if cut > 20 else s[:max_len]
    return s or "untitled"


def _matched_keywords(text: str) -> list[str]:
    """Return the subset of FILTER_KEYWORDS that appear (case-insensitive)
    in ``text``."""
    lower = text.lower()
    return [kw for kw in FILTER_KEYWORDS if kw.lower() in lower]


def _parse_pubdate(s: Optional[str]) -> Optional[str]:
    """Parse an RFC 2822 / RFC 822 (RSS) or ISO 8601 (Atom) date string into
    ISO-8601 UTC.

    Returns None if the string is missing or unparseable.
    """
    if not s or not s.strip():
        return None
    raw = s.strip()
    # Try RFC 2822 first (RSS standard).
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, IndexError):
        pass
    # Try ISO 8601 (Atom standard). datetime.fromisoformat in 3.11+ accepts
    # most variants but NOT the trailing 'Z' — normalise it.
    try:
        normalized = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


# Atom 1.0 namespace. RSS 2.0 has no namespace by default.
_ATOM_NS = "http://www.w3.org/2005/Atom"
_ATOM_PREFIX = f"{{{_ATOM_NS}}}"


def _parse_rss_2_items(channel: ET.Element, outlet: OutletConfig) -> list[PressItem]:
    """Parse RSS 2.0 ``<channel><item>...`` children into PressItems."""
    items: list[PressItem] = []
    for item_el in channel.findall("item"):
        title = (item_el.findtext("title") or "").strip()
        description = (item_el.findtext("description") or "").strip()
        link = (item_el.findtext("link") or "").strip()
        pub_raw = item_el.findtext("pubDate") or item_el.findtext(
            "{http://purl.org/dc/elements/1.1/}date"
        )
        items.append(PressItem(
            title=title,
            description=description,
            url=link,
            pub_date_iso=_parse_pubdate(pub_raw),
            outlet_key=outlet.key,
            outlet_label=outlet.label,
        ))
    return items


def _parse_atom_entries(root: ET.Element, outlet: OutletConfig) -> list[PressItem]:
    """Parse Atom 1.0 ``<feed><entry>...`` children into PressItems."""
    items: list[PressItem] = []
    for entry in root.findall(f"{_ATOM_PREFIX}entry"):
        title = (
            (entry.findtext(f"{_ATOM_PREFIX}title") or "").strip()
        )
        # Atom <link> uses href; multiple links allowed — prefer rel="alternate"
        # or the first link without an explicit rel.
        link = ""
        for link_el in entry.findall(f"{_ATOM_PREFIX}link"):
            rel = link_el.get("rel", "alternate")
            href = link_el.get("href", "")
            if href and rel in ("alternate", ""):
                link = href
                break
        # Body: prefer <summary>, fall back to <content>.
        description = (
            (entry.findtext(f"{_ATOM_PREFIX}summary") or "").strip()
            or (entry.findtext(f"{_ATOM_PREFIX}content") or "").strip()
        )
        pub_raw = (
            entry.findtext(f"{_ATOM_PREFIX}published")
            or entry.findtext(f"{_ATOM_PREFIX}updated")
        )
        items.append(PressItem(
            title=title,
            description=description,
            url=link,
            pub_date_iso=_parse_pubdate(pub_raw),
            outlet_key=outlet.key,
            outlet_label=outlet.label,
        ))
    return items


def parse_rss_xml(xml_text: str, outlet: OutletConfig) -> list[PressItem]:
    """Parse an RSS 2.0 OR Atom 1.0 feed body into PressItem objects (unfiltered).

    Detects the format by the root element:
    * ``<rss><channel><item>...`` → RSS 2.0
    * ``<feed xmlns="...Atom"><entry>...`` → Atom 1.0

    Caller applies :func:`is_relevant` to filter.
    """
    root = ET.fromstring(xml_text)
    # Atom: root tag carries the namespace prefix.
    if root.tag == f"{_ATOM_PREFIX}feed":
        return _parse_atom_entries(root, outlet)
    # RSS 2.0.
    channel = root.find("channel")
    if channel is not None:
        return _parse_rss_2_items(channel, outlet)
    # Fallback — try both. Some feeds wrap RSS in unusual root elements.
    items = _parse_atom_entries(root, outlet)
    if items:
        return items
    return []


def is_relevant(item: PressItem) -> bool:
    """Apply the Aschenbrenner / SA LP / Shulman / Trammell keyword filter.

    Sets ``item.matched_keywords`` as a side-effect when a match is found.
    """
    combined = f"{item.title}\n{item.description}"
    matches = _matched_keywords(combined)
    if matches:
        item.matched_keywords = matches
        return True
    return False


# ---------------------------------------------------------------------------
# Idempotency — scan existing files for ``url:`` frontmatter
# ---------------------------------------------------------------------------


_FRONTMATTER_URL_RE = re.compile(r"^url:\s*(.+?)\s*$", re.MULTILINE)


def _existing_urls(output_dir: Path) -> set[str]:
    """Scan ``output_dir`` for *.md files and extract their frontmatter
    ``url:`` values. Returns the set of URLs already represented on disk.
    """
    seen: set[str] = set()
    if not output_dir.exists():
        return seen
    for path in output_dir.glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Only the YAML frontmatter (between the first --- pair) is in scope.
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end < 0:
            continue
        head = text[:end]
        for m in _FRONTMATTER_URL_RE.finditer(head):
            url = m.group(1).strip().strip('"\'')
            if url:
                seen.add(url)
    return seen


# ---------------------------------------------------------------------------
# Markdown output
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def render_markdown(item: PressItem, *, ingested_iso: Optional[str] = None) -> str:
    """Render a PressItem as a markdown artifact matching the seeded shape.

    Schema mirrors the curated press files in ``ledgers/thematic/corpus/press/``
    (e.g. ``2026-05-24-aschenbrenner-fortune-october-2025.md``). The body
    contains the RSS-feed description verbatim plus a pointer to the URL —
    NOT the full article body (deliberately, per Session 5 design choice:
    most Tier-1 outlets are paywalled).
    """
    ingested = ingested_iso or _today_iso()
    created = item.pub_date_iso[:10] if item.pub_date_iso else ingested
    tags = ["ai", "aschenbrenner", "situational-awareness-lp", item.outlet_key]
    for kw in item.matched_keywords:
        kw_tag = kw.lower().replace(" ", "-")
        if kw_tag not in tags:
            tags.append(kw_tag)

    title_escaped = item.title.replace('"', '\\"')

    # YAML-safe URL (quote if it contains commas or other YAML-sensitive chars)
    url_yaml = item.url.replace('"', '\\"')

    frontmatter = (
        "---\n"
        "type: source\n"
        f"created: {created}\n"
        f"ingested: {ingested}\n"
        f'title: "{title_escaped}"\n'
        "author: ~\n"
        f'url: "{url_yaml}"\n'
        "raw_path: rss (press_rss fetcher — title + RSS description only, not full body)\n"
        "kind: article\n"
        f"tags: [{', '.join(tags)}]\n"
        "scope: cross\n"
        "---\n"
    )

    body = (
        f"\n# {item.outlet_label} — {item.title}\n\n"
        f"> Source: press_rss auto-fetch {_utc_now_iso()}; "
        f"keywords matched: {', '.join(item.matched_keywords) or '(none)'}\n\n"
        f"## RSS description\n\n"
        f"{item.description or '(empty)'}\n\n"
        f"## URL\n\n"
        f"<{item.url}>\n\n"
        "## Note\n\n"
        "Auto-fetched by press_rss. RSS description only — Loop 1 can WebFetch "
        "the full article body if the matched keywords + outlet warrant it. "
        f"Tier-1 outlets like {item.outlet_label} are often paywalled; the "
        "fetcher deliberately does not attempt to bypass paywalls.\n"
    )
    return frontmatter + body


def _output_filename(item: PressItem) -> str:
    """Build a filename for one press item.

    Format: ``YYYY-MM-DD-<outlet>-<title-slug>.md``. YYYY-MM-DD is the
    item's pub_date if present (so re-runs land in the same filename),
    else today.
    """
    date_part = item.pub_date_iso[:10] if item.pub_date_iso else _today_iso()
    slug = _slugify(item.title)
    outlet = item.outlet_key
    return f"{date_part}-{outlet}-{slug}.md"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _default_http_get(url: str, *, timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS) -> str:
    """Fetch a URL with a polite User-Agent. Returns body text.

    Raises :class:`urllib.error.URLError` on failure; caller wraps.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; Claude1 thematic-portfolio press_rss; "
                "+research/swing-trading-paper-portfolio)"
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    # Try UTF-8 first; fall back to latin-1 for legacy feeds.
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


# ---------------------------------------------------------------------------
# Top-level entry points
# ---------------------------------------------------------------------------


def fetch_one_outlet(
    outlet: OutletConfig,
    *,
    http_get=_default_http_get,
    timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
) -> tuple[list[PressItem], Optional[str]]:
    """Fetch + parse one outlet. Returns (items, error_msg)."""
    try:
        body = http_get(outlet.rss_url, timeout=timeout)
    except (urllib.error.URLError, OSError) as exc:
        return [], f"http_fetch_failed: {exc}"
    try:
        items = parse_rss_xml(body, outlet)
    except ET.ParseError as exc:
        return [], f"rss_parse_failed: {exc}"
    return items, None


def fetch_and_save(
    *,
    outlets: Optional[list[str]] = None,
    since: Optional[str] = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    dry_run: bool = False,
    http_get=_default_http_get,
    timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
) -> TraceEntry:
    """End-to-end: poll outlets, filter, write artifacts.

    Args:
        outlets: list of outlet keys (e.g. ``["fortune", "semafor"]``).
            Default = all outlets in :data:`OUTLET_CATALOG`.
        since: ISO-8601 cutoff (e.g. ``"2026-05-01"``). Items with
            ``pub_date_iso < since`` are skipped. Items with no pub_date
            are kept (when in doubt, ingest — the dedupe check catches
            true duplicates).
        output_dir: where to write the markdown artifacts.
        dry_run: when True, no files are written. Returned counts still
            reflect what WOULD have been written.
        http_get: injection point for tests.
        timeout: per-request fetch timeout (seconds).
    """
    if outlets is None:
        outlet_keys = list(OUTLET_CATALOG.keys())
    else:
        outlet_keys = []
        for k in outlets:
            if k not in OUTLET_CATALOG:
                raise ValueError(
                    f"Unknown outlet {k!r}; supported: {list(OUTLET_CATALOG)}"
                )
            outlet_keys.append(k)

    existing_urls = _existing_urls(output_dir)

    since_dt: Optional[datetime] = None
    if since:
        cleaned = since.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            since_dt = datetime.fromisoformat(cleaned)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            raise ValueError(f"--since must be ISO-8601; got {since!r}") from None

    written_paths: list[str] = []
    items_summary: list[dict[str, Any]] = []
    n_matched = 0
    n_skipped_dup = 0
    n_skipped_old = 0
    errors: list[dict[str, str]] = []

    for key in outlet_keys:
        outlet = OUTLET_CATALOG[key]
        items, err = fetch_one_outlet(outlet, http_get=http_get, timeout=timeout)
        if err:
            errors.append({"outlet": key, "error": err})
            continue
        for item in items:
            if not is_relevant(item):
                continue
            n_matched += 1
            if since_dt and item.pub_date_iso:
                item_dt = datetime.fromisoformat(item.pub_date_iso)
                if item_dt < since_dt:
                    n_skipped_old += 1
                    continue
            if item.url and item.url in existing_urls:
                n_skipped_dup += 1
                continue

            filename = _output_filename(item)
            target = output_dir / filename
            if not dry_run:
                output_dir.mkdir(parents=True, exist_ok=True)
                target.write_text(render_markdown(item), encoding="utf-8")
                # Update in-memory set so subsequent items in the same run
                # don't conflict if two outlets surface the same URL.
                if item.url:
                    existing_urls.add(item.url)
            written_paths.append(str(target))
            items_summary.append({
                "outlet": item.outlet_key,
                "title": item.title,
                "url": item.url,
                "pub_date": item.pub_date_iso,
                "matched_keywords": item.matched_keywords,
                "filename": filename,
            })

    result = FetchAndSaveResult(
        n_outlets_polled=len(outlet_keys),
        n_items_matched=n_matched,
        n_items_written=len(written_paths),
        n_items_skipped_duplicate=n_skipped_dup,
        n_items_skipped_old=n_skipped_old,
        n_outlets_errored=len(errors),
        written_paths=written_paths,
        errors=errors,
        items=items_summary,
    )

    return TraceEntry(
        tool=TOOL,
        inputs={
            "outlets": outlet_keys,
            "since": since,
            "output_dir": str(output_dir),
            "dry_run": dry_run,
        },
        output=result.to_dict(),
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.corpus.press_rss",
        description=(
            "Pull Tier-1 press outlet RSS feeds, filter for Aschenbrenner / "
            "SA LP / Shulman / Trammell mentions, write markdown into the "
            "thematic-portfolio corpus directory."
        ),
    )
    p.add_argument(
        "--outlets",
        type=str,
        default=None,
        help=f"Comma-separated outlet keys. Default = all of {list(OUTLET_CATALOG)}.",
    )
    p.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO-8601 date or datetime cutoff. Items older are skipped.",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory. Default: ledgers/thematic/corpus/press/.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip file writes. Report counts only.",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_FETCH_TIMEOUT_SECONDS,
        help=f"Per-request fetch timeout in seconds. Default: {DEFAULT_FETCH_TIMEOUT_SECONDS}.",
    )
    args = p.parse_args()

    outlets = args.outlets.split(",") if args.outlets else None

    entry = fetch_and_save(
        outlets=outlets,
        since=args.since,
        output_dir=args.output_dir,
        dry_run=args.dry_run,
        timeout=args.timeout,
    )
    emit(entry)


if __name__ == "__main__":
    main()
