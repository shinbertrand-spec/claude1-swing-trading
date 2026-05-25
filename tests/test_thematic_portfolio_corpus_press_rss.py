"""Tests for tools.thematic_portfolio.corpus.press_rss.

Covers:
  * RSS 2.0 parse shape (title / description / link / pubDate extraction)
  * Keyword filter (Aschenbrenner / Situational Awareness / Shulman / Trammell)
  * Date parsing (RFC 2822 -> ISO-8601 UTC)
  * Slug generation
  * Markdown frontmatter shape matches the seeded press files
  * Idempotency via existing-URL scan of output dir
  * --since cutoff filtering
  * Multi-outlet aggregation
  * Outlet error isolation (one outlet's HTTP fail doesn't abort the others)
  * Dry-run does not write files
  * No-pubDate items are kept (not skipped by --since)
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tools.thematic_portfolio.corpus.press_rss import (
    FILTER_KEYWORDS,
    OUTLET_CATALOG,
    PressItem,
    _existing_urls,
    _output_filename,
    _slugify,
    _parse_pubdate,
    fetch_and_save,
    is_relevant,
    parse_rss_xml,
    render_markdown,
)


def _rss(items_xml: str, channel_title: str = "Test Feed") -> str:
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <title>{channel_title}</title>
            <link>https://example.test/</link>
            <description>x</description>
            {items_xml}
          </channel>
        </rss>
        """)


def _item(
    title="Hello world",
    desc="Some description",
    link="https://example.test/post1",
    pub="Mon, 25 May 2026 10:00:00 +0000",
):
    pub_block = f"<pubDate>{pub}</pubDate>" if pub is not None else ""
    return f"""\
        <item>
          <title>{title}</title>
          <description><![CDATA[{desc}]]></description>
          <link>{link}</link>
          {pub_block}
        </item>
    """


# --- parse_rss_xml ---------------------------------------------------------


def test_parse_rss_xml_extracts_fields():
    outlet = OUTLET_CATALOG["fortune"]
    xml = _rss(_item(
        title="A piece on Aschenbrenner",
        desc="Inside SA LP",
        link="https://fortune.com/2026/abc",
        pub="Mon, 25 May 2026 10:00:00 +0000",
    ))
    items = parse_rss_xml(xml, outlet)
    assert len(items) == 1
    it = items[0]
    assert it.title == "A piece on Aschenbrenner"
    assert it.description == "Inside SA LP"
    assert it.url == "https://fortune.com/2026/abc"
    assert it.pub_date_iso == "2026-05-25T10:00:00+00:00"
    assert it.outlet_key == "fortune"


def test_parse_rss_xml_empty_channel():
    outlet = OUTLET_CATALOG["fortune"]
    xml = _rss("")
    assert parse_rss_xml(xml, outlet) == []


def test_parse_rss_xml_no_pubdate_yields_none():
    outlet = OUTLET_CATALOG["fortune"]
    xml = _rss(_item(pub=None))
    items = parse_rss_xml(xml, outlet)
    assert items[0].pub_date_iso is None


def test_parse_rss_xml_handles_multiple_items():
    outlet = OUTLET_CATALOG["fortune"]
    xml = _rss(
        _item(title="A", link="https://fortune.com/a")
        + _item(title="B", link="https://fortune.com/b")
    )
    items = parse_rss_xml(xml, outlet)
    assert [i.title for i in items] == ["A", "B"]


# --- is_relevant -----------------------------------------------------------


def test_is_relevant_title_match():
    item = PressItem(
        title="Aschenbrenner raises $5B",
        description="unrelated",
        url="https://x.test",
        pub_date_iso=None,
        outlet_key="fortune",
        outlet_label="Fortune",
    )
    assert is_relevant(item) is True
    assert item.matched_keywords == ["Aschenbrenner"]


def test_is_relevant_description_match():
    item = PressItem(
        title="AI hedge funds 2026",
        description="Carl Shulman discusses risk",
        url="https://x.test",
        pub_date_iso=None,
        outlet_key="semafor",
        outlet_label="Semafor",
    )
    assert is_relevant(item) is True
    assert item.matched_keywords == ["Carl Shulman"]


def test_is_relevant_case_insensitive():
    item = PressItem(
        title="philip trammell paper review",
        description="",
        url="https://x.test",
        pub_date_iso=None,
        outlet_key="fortune",
        outlet_label="Fortune",
    )
    assert is_relevant(item) is True


def test_is_relevant_no_match():
    item = PressItem(
        title="Fed minutes leak",
        description="",
        url="https://x.test",
        pub_date_iso=None,
        outlet_key="fortune",
        outlet_label="Fortune",
    )
    assert is_relevant(item) is False
    assert item.matched_keywords == []


def test_is_relevant_situational_awareness_phrase():
    item = PressItem(
        title="Situational Awareness fund returns",
        description="",
        url="https://x.test",
        pub_date_iso=None,
        outlet_key="fortune",
        outlet_label="Fortune",
    )
    assert is_relevant(item) is True
    assert "Situational Awareness" in item.matched_keywords


def test_filter_keywords_locked_set():
    # Locking the set so any addition is intentional + reviewed.
    assert set(FILTER_KEYWORDS) == {
        "Aschenbrenner",
        "Situational Awareness",
        "Carl Shulman",
        "Philip Trammell",
    }


# --- date parsing ----------------------------------------------------------


@pytest.mark.parametrize("rfc822,expected", [
    ("Mon, 25 May 2026 10:00:00 +0000", "2026-05-25T10:00:00+00:00"),
    ("Tue, 01 Jan 2026 00:00:00 GMT", "2026-01-01T00:00:00+00:00"),
    # PDT offset normalised to UTC
    ("Mon, 25 May 2026 03:00:00 -0700", "2026-05-25T10:00:00+00:00"),
])
def test_parse_pubdate_normalises_to_utc_iso(rfc822, expected):
    assert _parse_pubdate(rfc822) == expected


def test_parse_pubdate_invalid_returns_none():
    assert _parse_pubdate("not a date") is None
    assert _parse_pubdate("") is None
    assert _parse_pubdate(None) is None


# --- slugify ---------------------------------------------------------------


def test_slugify_lowercase_kebab():
    assert _slugify("A Piece on Aschenbrenner") == "a-piece-on-aschenbrenner"


def test_slugify_strips_punctuation():
    assert _slugify("Aschenbrenner: $5B fund.") == "aschenbrenner-5b-fund"


def test_slugify_truncates_long_titles():
    long = "this is a deliberately very long title that goes on and on and should be cut off cleanly"
    out = _slugify(long, max_len=40)
    assert len(out) <= 40
    assert not out.endswith("-")


def test_slugify_empty_fallback():
    assert _slugify("") == "untitled"
    assert _slugify("!!!") == "untitled"


# --- output filename -------------------------------------------------------


def test_output_filename_uses_pub_date():
    item = PressItem(
        title="Aschenbrenner profile",
        description="",
        url="https://fortune.com/x",
        pub_date_iso="2026-05-25T10:00:00+00:00",
        outlet_key="fortune",
        outlet_label="Fortune",
    )
    assert _output_filename(item) == "2026-05-25-fortune-aschenbrenner-profile.md"


def test_output_filename_falls_back_to_today_when_no_pub_date():
    item = PressItem(
        title="Aschenbrenner profile",
        description="",
        url="https://x.test",
        pub_date_iso=None,
        outlet_key="semafor",
        outlet_label="Semafor",
    )
    fn = _output_filename(item)
    # Today's date prefix + outlet + slug
    assert fn.endswith("-semafor-aschenbrenner-profile.md")
    # Day prefix is the run-time date — just verify the rough shape
    assert len(fn.split("-")[0]) == 4  # YYYY


# --- render_markdown -------------------------------------------------------


def test_render_markdown_frontmatter_shape():
    item = PressItem(
        title="Inside SA LP",
        description="A profile",
        url="https://fortune.com/x",
        pub_date_iso="2026-05-25T10:00:00+00:00",
        outlet_key="fortune",
        outlet_label="Fortune",
        matched_keywords=["Aschenbrenner", "Situational Awareness"],
    )
    md = render_markdown(item, ingested_iso="2026-05-28")
    # Frontmatter present + opens with type: source (matches seeded files)
    assert md.startswith("---\n")
    assert "type: source\n" in md
    assert "created: 2026-05-25\n" in md  # from pub_date
    assert "ingested: 2026-05-28\n" in md
    assert 'title: "Inside SA LP"\n' in md
    assert 'url: "https://fortune.com/x"\n' in md
    assert "kind: article\n" in md
    assert "scope: cross\n" in md
    # Tags include outlet + matched keywords
    assert "fortune" in md
    assert "aschenbrenner" in md
    assert "situational-awareness" in md
    # Body contains the RSS description + URL
    assert "A profile" in md
    assert "<https://fortune.com/x>" in md


def test_render_markdown_escapes_quotes_in_title():
    item = PressItem(
        title='Profile: "SA LP" hedge fund',
        description="",
        url="https://x.test",
        pub_date_iso=None,
        outlet_key="fortune",
        outlet_label="Fortune",
        matched_keywords=["Aschenbrenner"],
    )
    md = render_markdown(item)
    # Escaped backslash + quote in YAML title
    assert '\\"' in md.split("title:")[1].split("\n", 1)[0]


# --- idempotency ----------------------------------------------------------


def test_existing_urls_scans_frontmatter(tmp_path):
    (tmp_path / "a.md").write_text(
        '---\nurl: "https://fortune.com/old1"\nscope: cross\n---\nbody\n',
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        '---\nurl: https://semafor.com/old2\n---\nbody\n',
        encoding="utf-8",
    )
    # Non-md file ignored
    (tmp_path / "c.txt").write_text("url: https://ignored", encoding="utf-8")
    # md file without frontmatter ignored
    (tmp_path / "d.md").write_text("no frontmatter here\nurl: https://nope", encoding="utf-8")

    urls = _existing_urls(tmp_path)
    assert urls == {
        "https://fortune.com/old1",
        "https://semafor.com/old2",
    }


def test_existing_urls_handles_missing_dir(tmp_path):
    assert _existing_urls(tmp_path / "missing") == set()


# --- fetch_and_save -------------------------------------------------------


def _fake_http(responses: dict[str, str]):
    """Build a fake http_get fn that returns canned XML per URL."""
    def fn(url, *, timeout=None):
        if url not in responses:
            from urllib.error import URLError
            raise URLError(f"unexpected URL: {url}")
        return responses[url]
    return fn


def test_fetch_and_save_writes_one_matching_item(tmp_path):
    rss = _rss(_item(
        title="Inside SA LP — Aschenbrenner profile",
        desc="An inside look",
        link="https://fortune.com/new1",
        pub="Mon, 25 May 2026 10:00:00 +0000",
    ))
    http = _fake_http({OUTLET_CATALOG["fortune"].rss_url: rss})
    entry = fetch_and_save(
        outlets=["fortune"],
        output_dir=tmp_path,
        http_get=http,
    )
    out = entry.output
    assert out["n_items_matched"] == 1
    assert out["n_items_written"] == 1
    assert out["n_items_skipped_duplicate"] == 0
    assert out["n_outlets_errored"] == 0

    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert 'url: "https://fortune.com/new1"' in text


def test_fetch_and_save_filters_non_matching_items(tmp_path):
    rss = _rss(
        _item(title="Fed minutes leak", desc="unrelated", link="https://fortune.com/skip1")
        + _item(title="Aschenbrenner raises capital", desc="", link="https://fortune.com/keep1")
    )
    http = _fake_http({OUTLET_CATALOG["fortune"].rss_url: rss})
    entry = fetch_and_save(
        outlets=["fortune"],
        output_dir=tmp_path,
        http_get=http,
    )
    assert entry.output["n_items_matched"] == 1
    assert len(list(tmp_path.glob("*.md"))) == 1


def test_fetch_and_save_dedupes_existing_url(tmp_path):
    # Seed an existing file with the URL we'd otherwise pick up.
    (tmp_path / "seed.md").write_text(
        '---\nurl: "https://fortune.com/dup1"\n---\n',
        encoding="utf-8",
    )
    rss = _rss(_item(
        title="Aschenbrenner repeat post",
        link="https://fortune.com/dup1",
    ))
    http = _fake_http({OUTLET_CATALOG["fortune"].rss_url: rss})
    entry = fetch_and_save(
        outlets=["fortune"],
        output_dir=tmp_path,
        http_get=http,
    )
    out = entry.output
    assert out["n_items_matched"] == 1
    assert out["n_items_written"] == 0
    assert out["n_items_skipped_duplicate"] == 1
    # Only the seed file remains
    assert {p.name for p in tmp_path.glob("*.md")} == {"seed.md"}


def test_fetch_and_save_since_filter(tmp_path):
    rss = _rss(
        _item(
            title="Aschenbrenner 2025 piece",
            link="https://fortune.com/old",
            pub="Mon, 01 Jan 2025 00:00:00 +0000",
        )
        + _item(
            title="Aschenbrenner 2026 piece",
            link="https://fortune.com/new",
            pub="Mon, 25 May 2026 10:00:00 +0000",
        )
    )
    http = _fake_http({OUTLET_CATALOG["fortune"].rss_url: rss})
    entry = fetch_and_save(
        outlets=["fortune"],
        since="2026-01-01",
        output_dir=tmp_path,
        http_get=http,
    )
    out = entry.output
    assert out["n_items_matched"] == 2
    assert out["n_items_written"] == 1
    assert out["n_items_skipped_old"] == 1


def test_fetch_and_save_multi_outlet_aggregates(tmp_path):
    rss_fortune = _rss(_item(
        title="Aschenbrenner profile",
        link="https://fortune.com/x",
    ))
    rss_semafor = _rss(_item(
        title="Carl Shulman interview",
        link="https://semafor.com/y",
    ))
    http = _fake_http({
        OUTLET_CATALOG["fortune"].rss_url: rss_fortune,
        OUTLET_CATALOG["semafor"].rss_url: rss_semafor,
    })
    entry = fetch_and_save(
        outlets=["fortune", "semafor"],
        output_dir=tmp_path,
        http_get=http,
    )
    out = entry.output
    assert out["n_outlets_polled"] == 2
    assert out["n_items_written"] == 2
    filenames = {Path(p).name for p in out["written_paths"]}
    assert any("fortune" in n for n in filenames)
    assert any("semafor" in n for n in filenames)


def test_fetch_and_save_one_outlet_error_does_not_abort_others(tmp_path):
    from urllib.error import URLError

    def http(url, *, timeout=None):
        if url == OUTLET_CATALOG["fortune"].rss_url:
            raise URLError("network down")
        if url == OUTLET_CATALOG["semafor"].rss_url:
            return _rss(_item(
                title="Aschenbrenner update",
                link="https://semafor.com/x",
            ))
        raise URLError("unexpected")

    entry = fetch_and_save(
        outlets=["fortune", "semafor"],
        output_dir=tmp_path,
        http_get=http,
    )
    out = entry.output
    assert out["n_outlets_errored"] == 1
    assert out["n_items_written"] == 1  # semafor succeeded
    assert any("fortune" in e["outlet"] for e in out["errors"])
    assert "http_fetch_failed" in out["errors"][0]["error"]


def test_fetch_and_save_dry_run_does_not_write(tmp_path):
    rss = _rss(_item(
        title="Aschenbrenner piece",
        link="https://fortune.com/x",
    ))
    http = _fake_http({OUTLET_CATALOG["fortune"].rss_url: rss})
    entry = fetch_and_save(
        outlets=["fortune"],
        output_dir=tmp_path,
        dry_run=True,
        http_get=http,
    )
    out = entry.output
    assert out["n_items_matched"] == 1
    assert out["n_items_written"] == 1  # counted but...
    assert list(tmp_path.glob("*.md")) == []  # ...no file written


def test_fetch_and_save_unknown_outlet_raises():
    with pytest.raises(ValueError, match="Unknown outlet"):
        fetch_and_save(outlets=["nonexistent"], output_dir=Path("/tmp"))


def test_fetch_and_save_no_pubdate_items_kept_under_since(tmp_path):
    """An item without a pubDate should not be filtered by --since
    (when in doubt, ingest)."""
    rss = _rss(_item(
        title="Aschenbrenner piece",
        link="https://fortune.com/x",
        pub=None,
    ))
    http = _fake_http({OUTLET_CATALOG["fortune"].rss_url: rss})
    entry = fetch_and_save(
        outlets=["fortune"],
        since="2030-01-01",  # far in the future
        output_dir=tmp_path,
        http_get=http,
    )
    assert entry.output["n_items_written"] == 1


def test_fetch_and_save_intra_run_dedupe(tmp_path):
    """Same URL appearing in two outlets in one run: write once, skip on
    second sighting."""
    same_url = "https://example.com/cross-syndicated"
    rss = _rss(_item(title="Aschenbrenner piece", link=same_url))
    http = _fake_http({
        OUTLET_CATALOG["fortune"].rss_url: rss,
        OUTLET_CATALOG["semafor"].rss_url: rss,
    })
    entry = fetch_and_save(
        outlets=["fortune", "semafor"],
        output_dir=tmp_path,
        http_get=http,
    )
    out = entry.output
    assert out["n_items_matched"] == 2
    assert out["n_items_written"] == 1
    assert out["n_items_skipped_duplicate"] == 1


def test_fetch_and_save_returns_trace_entry(tmp_path):
    http = _fake_http({OUTLET_CATALOG["fortune"].rss_url: _rss("")})
    entry = fetch_and_save(outlets=["fortune"], output_dir=tmp_path, http_get=http)
    assert entry.tool == "tools/thematic_portfolio/corpus/press_rss.py"
    assert entry.inputs["outlets"] == ["fortune"]
    assert entry.inputs["output_dir"] == str(tmp_path)
    assert entry.fetched_at
