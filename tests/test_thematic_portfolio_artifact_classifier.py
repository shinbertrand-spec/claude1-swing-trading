"""Tests for tools.thematic_portfolio.artifact_classifier.

Covers: pre-filter deterministic rules, LLM-verdict finalization, mandatory-
escalation signal scanning, rate-limit + firing-log state management.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.thematic_portfolio.artifact_classifier import (
    MAX_FIRINGS_PER_WEEK,
    PRESS_TOUR_BURST_THRESHOLD,
    ArtifactInput,
    ClassificationResult,
    apply_rate_limit,
    classify_pipeline,
    finalize_from_llm_verdict,
    load_firing_log,
    pre_filter,
    record_firing,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _hours_ago(n: float) -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(hours=n))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _artifact(
    *,
    source: str = "x:@leopoldasch",
    snippet: str = "x" * 200,
    url: str | None = None,
    is_thread: bool = False,
    thread_total_words: int = 0,
    author_handle: str | None = "@leopoldasch",
    fetched_at: str | None = None,
) -> ArtifactInput:
    return ArtifactInput(
        source=source,
        snippet=snippet,
        snippet_length_chars=len(snippet),
        url=url,
        is_thread=is_thread,
        thread_total_words=thread_total_words,
        author_handle=author_handle,
        fetched_at=fetched_at or _now_iso(),
    )


# ---------------------------------------------------------------------------
# Pre-filter Tier 1 (auto-trigger sources)
# ---------------------------------------------------------------------------


def test_pre_filter_13f_filing_is_tier1():
    r = pre_filter(_artifact(source="13f:sa_lp", snippet="...filing snippet..."))
    assert r is not None
    assert r.tier == 1
    assert r.matched_rule == "tier_1_filing"
    assert r.would_fire_loop1 is True
    assert r.pre_filter_decided is True


def test_pre_filter_regulatory_filing_is_tier1():
    r = pre_filter(_artifact(source="regulatory:congressional_testimony", snippet="."))
    assert r is not None
    assert r.tier == 1
    assert r.matched_rule == "tier_1_regulatory"


def test_pre_filter_essay_at_tier1_venue_is_tier1():
    r = pre_filter(
        _artifact(
            source="essay:forourposterity",
            url="https://forourposterity.com/2026/06/post.html",
            snippet="A new essay by Aschenbrenner about chips.",
        )
    )
    assert r is not None
    assert r.tier == 1
    assert r.matched_rule == "tier_1_essay"


def test_pre_filter_essay_at_unknown_venue_returns_none():
    """Substack-style venue not in TIER1_ESSAY_VENUES → pre-filter cannot decide."""
    r = pre_filter(
        _artifact(
            source="essay:randomsubstack",
            url="https://randomwriter.substack.com/p/post",
            snippet="A piece about Aschenbrenner.",
        )
    )
    assert r is None  # boundary case → LLM


def test_pre_filter_dwarkesh_podcast_is_tier1():
    r = pre_filter(_artifact(source="podcast:dwarkesh-2026-05", snippet="."))
    assert r is not None
    assert r.tier == 1
    assert r.matched_rule == "tier_1_podcast"


def test_pre_filter_bg2_podcast_is_tier1():
    r = pre_filter(_artifact(source="podcast:bg2-ep-105", snippet="."))
    assert r is not None
    assert r.matched_rule == "tier_1_podcast"


def test_pre_filter_random_podcast_returns_none():
    """Podcast not in TIER1_PODCAST_TOKENS → LLM judgment."""
    r = pre_filter(_artifact(source="podcast:my-random-show", snippet="."))
    assert r is None


def test_pre_filter_fortune_press_is_tier1():
    r = pre_filter(
        _artifact(
            source="press:fortune.com",
            url="https://fortune.com/2026/03/sa-lp-profile",
            snippet="...",
        )
    )
    assert r is not None
    assert r.tier == 1
    assert r.matched_rule == "tier_1_press"


def test_pre_filter_lower_tier_press_is_tier3():
    """Motley Fool / ZeroHedge / aggregator → Tier 3 lower-tier press."""
    r = pre_filter(
        _artifact(
            source="press:motleyfool.com",
            url="https://motleyfool.com/article",
            snippet=".",
        )
    )
    assert r is not None
    assert r.tier == 3
    assert r.matched_rule == "tier_3_lower_tier_press"
    assert r.would_fire_loop1 is False


# ---------------------------------------------------------------------------
# Pre-filter Tier 3 (hard excludes)
# ---------------------------------------------------------------------------


def test_pre_filter_x_post_non_listed_author_is_tier3():
    r = pre_filter(
        _artifact(
            source="x:@randomuser",
            author_handle="@randomuser",
            snippet="A long post about Aschenbrenner that's over 150 chars" + "x" * 200,
        )
    )
    assert r is not None
    assert r.tier == 3
    assert r.matched_rule == "tier_3_other"


def test_pre_filter_x_post_short_from_listed_author_is_tier3():
    r = pre_filter(
        _artifact(
            source="x:@leopoldasch",
            author_handle="@leopoldasch",
            snippet="short post",
        )
    )
    assert r is not None
    assert r.tier == 3
    assert r.matched_rule == "tier_3_short_post"


def test_pre_filter_x_post_long_from_listed_author_returns_none():
    """≥150 chars from listed author → LLM resolves Tier 2 vs 2.5 boundary."""
    r = pre_filter(
        _artifact(
            source="x:@leopoldasch",
            author_handle="@leopoldasch",
            snippet="A" * 200,
        )
    )
    assert r is None


def test_pre_filter_short_x_post_with_long_thread_returns_none():
    """A short post inside a ≥500-word thread bypasses the short-post exclusion."""
    r = pre_filter(
        _artifact(
            source="x:@leopoldasch",
            author_handle="@leopoldasch",
            snippet="short post text",
            is_thread=True,
            thread_total_words=600,
        )
    )
    assert r is None  # → LLM


def test_pre_filter_carl_shulman_handle_listed():
    r = pre_filter(
        _artifact(
            source="x:@CarlShulman",
            author_handle="@CarlShulman",
            snippet="A" * 200,
        )
    )
    assert r is None  # ≥150 chars from Shulman → LLM


def test_pre_filter_adjacent_fund_press_is_tier3():
    r = pre_filter(_artifact(source="press_adjacent_fund:altimeter-coverage", snippet="."))
    assert r is not None
    assert r.tier == 3
    assert r.matched_rule == "tier_3_adjacent_press"


def test_pre_filter_critic_essay_is_tier3():
    r = pre_filter(_artifact(source="press_critic:marcus-substack", snippet="."))
    assert r is not None
    assert r.tier == 3
    assert r.matched_rule == "tier_3_critic_essay"


def test_pre_filter_wider_ai_policy_is_tier3():
    r = pre_filter(_artifact(source="policy:executive-order-ai-2026", snippet="."))
    assert r is not None
    assert r.tier == 3
    assert r.matched_rule == "tier_3_wider_policy"


def test_pre_filter_unknown_source_returns_none():
    """Unknown source pattern → refuse to guess, defer to LLM."""
    r = pre_filter(_artifact(source="other:weird-source", author_handle=None, snippet="."))
    assert r is None


# ---------------------------------------------------------------------------
# Mandatory-escalation signals (surfaced regardless of tier)
# ---------------------------------------------------------------------------


def test_pre_filter_surfaces_thesis_abandonment_signal():
    r = pre_filter(
        _artifact(
            source="13f:sa_lp",
            snippet="we've exited the SNDK long; cash-raise primary",
        )
    )
    assert r is not None
    sigs = r.mandatory_escalation_signals
    assert any(s["signal_type"] == "thesis_abandonment" for s in sigs)


def test_pre_filter_surfaces_strategy_pivot_signal():
    r = pre_filter(
        _artifact(
            source="13f:sa_lp",
            snippet="we've pivoted from chip-shorts to chip-longs",
        )
    )
    sigs = r.mandatory_escalation_signals
    assert any(s["signal_type"] == "strategy_pivot" for s in sigs)


def test_pre_filter_surfaces_sa_lp_event_signal():
    r = pre_filter(
        _artifact(
            source="press:fortune.com",
            url="https://fortune.com/x",
            snippet="SA LP wind-down formally announced today",
        )
    )
    sigs = r.mandatory_escalation_signals
    assert any(s["signal_type"] == "sa_lp_event" for s in sigs)


# ---------------------------------------------------------------------------
# finalize_from_llm_verdict
# ---------------------------------------------------------------------------


def test_finalize_llm_tier2_fires():
    snippet = "Long post about SNDK with explicit ticker"
    r = finalize_from_llm_verdict(
        {"tier": 2, "reason": "ticker present", "matched_rule": "tier_2_x_with_ticker"},
        snippet,
    )
    assert r.tier == 2
    assert r.would_fire_loop1 is True
    assert r.pre_filter_decided is False


def test_finalize_llm_tier_25_does_not_fire():
    r = finalize_from_llm_verdict(
        {
            "tier": 25,
            "reason": "alignment philosophy post",
            "matched_rule": "tier_25_x_meets_length_misses_content",
        },
        "A philosophy post without specifics",
    )
    assert r.tier == 25
    assert r.would_fire_loop1 is False


def test_finalize_rejects_missing_keys():
    with pytest.raises(ValueError, match="missing required keys"):
        finalize_from_llm_verdict({"tier": 2, "reason": "incomplete"}, "snippet")


def test_finalize_rejects_invalid_tier():
    with pytest.raises(ValueError, match="not in"):
        finalize_from_llm_verdict(
            {"tier": 5, "reason": "x", "matched_rule": "y"}, "snippet"
        )


def test_finalize_unions_escalation_signals_from_llm_and_deterministic_scan():
    """LLM might miss a phrase the regex catches; we union both."""
    snippet = "we've exited the SNDK long. Going to cash."
    # LLM verdict misses the signal entirely
    llm_verdict = {
        "tier": 2,
        "reason": "thesis update",
        "matched_rule": "tier_2_x_with_thesis_update",
        "mandatory_escalation_signals": [],
    }
    r = finalize_from_llm_verdict(llm_verdict, snippet)
    assert any(
        s["signal_type"] == "thesis_abandonment" for s in r.mandatory_escalation_signals
    )


def test_finalize_dedupes_overlapping_signals():
    snippet = "we've exited the SNDK long."
    llm_verdict = {
        "tier": 2,
        "reason": "x",
        "matched_rule": "tier_2_x_with_thesis_update",
        "mandatory_escalation_signals": [
            {
                "signal_type": "thesis_abandonment",
                "matched_phrase": "we've exited",
                "severity": "high",
            }
        ],
    }
    r = finalize_from_llm_verdict(llm_verdict, snippet)
    # The deterministic scan would also produce the same signal; dedupe keeps one.
    thesis_signals = [
        s for s in r.mandatory_escalation_signals if s["signal_type"] == "thesis_abandonment"
    ]
    assert len(thesis_signals) == 1


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


def _make_fire_result(tier: int = 2, signals=None) -> ClassificationResult:
    return ClassificationResult(
        tier=tier,
        reason="test",
        matched_rule="tier_2_x_with_ticker",
        pre_filter_decided=False,
        would_fire_loop1=tier in (1, 2),
        mandatory_escalation_signals=signals or [],
    )


def test_rate_limit_fires_when_under_cap():
    r = _make_fire_result()
    decision = apply_rate_limit(r, {"firings": []}, _now_iso())
    assert decision.fired is True
    assert decision.rate_limited is False
    assert decision.firings_in_window == 0


def test_rate_limit_blocks_at_cap_without_escalation():
    r = _make_fire_result()
    log = {
        "firings": [
            {"fired_at": _hours_ago(1)},
            {"fired_at": _hours_ago(40)},
            {"fired_at": _hours_ago(80)},
        ]
    }
    decision = apply_rate_limit(r, log, _now_iso())
    assert decision.fired is False
    assert decision.rate_limited is True
    assert decision.mandatory_escalation_applied is False
    assert decision.firings_in_window == MAX_FIRINGS_PER_WEEK


def test_rate_limit_escalation_signal_overrides_cap():
    r = _make_fire_result(
        signals=[
            {
                "signal_type": "thesis_abandonment",
                "matched_phrase": "we've exited",
                "severity": "high",
            }
        ]
    )
    log = {
        "firings": [
            {"fired_at": _hours_ago(1)},
            {"fired_at": _hours_ago(40)},
            {"fired_at": _hours_ago(80)},
        ]
    }
    decision = apply_rate_limit(r, log, _now_iso())
    assert decision.fired is True
    assert decision.rate_limited is True  # cap was consumed
    assert decision.mandatory_escalation_applied is True


def test_rate_limit_press_tour_burst_overrides_cap():
    """≥3 firings in past 24h triggers mandatory escalation even without phrase signal."""
    r = _make_fire_result()
    log = {
        "firings": [
            {"fired_at": _hours_ago(2)},
            {"fired_at": _hours_ago(5)},
            {"fired_at": _hours_ago(10)},
        ]
    }
    decision = apply_rate_limit(r, log, _now_iso())
    assert decision.fired is True
    assert decision.mandatory_escalation_applied is True
    assert decision.burst_in_24h >= PRESS_TOUR_BURST_THRESHOLD


def test_rate_limit_old_firings_excluded():
    """Firings outside the 7-day window do NOT count."""
    r = _make_fire_result()
    log = {
        "firings": [
            {"fired_at": _hours_ago(24 * 8)},  # 8 days ago — out of window
            {"fired_at": _hours_ago(24 * 10)},
        ]
    }
    decision = apply_rate_limit(r, log, _now_iso())
    assert decision.fired is True
    assert decision.firings_in_window == 0


def test_rate_limit_tier_25_does_not_fire_regardless():
    r = ClassificationResult(
        tier=25,
        reason="alignment philosophy",
        matched_rule="tier_25_x_meets_length_misses_content",
        pre_filter_decided=False,
        would_fire_loop1=False,
    )
    decision = apply_rate_limit(r, {"firings": []}, _now_iso())
    assert decision.fired is False
    assert decision.rate_limited is False


def test_rate_limit_robust_to_malformed_log_entries():
    """A firing entry missing fired_at should be silently skipped, not crash."""
    r = _make_fire_result()
    log = {
        "firings": [
            {"fired_at": _hours_ago(1)},
            {"trigger_type": "broken_entry"},  # missing fired_at
            {"fired_at": "not-a-timestamp"},  # unparseable
            {"fired_at": _hours_ago(20)},
        ]
    }
    decision = apply_rate_limit(r, log, _now_iso())
    # The two valid entries count.
    assert decision.firings_in_window == 2
    assert decision.fired is True


# ---------------------------------------------------------------------------
# Firing log I/O
# ---------------------------------------------------------------------------


def test_load_firing_log_missing_returns_empty_schema(tmp_path: Path):
    log = load_firing_log(tmp_path / "doesnt-exist.json")
    assert log == {"schema_version": "1.0", "updated_at": None, "firings": []}


def test_record_firing_creates_parent_dirs(tmp_path: Path):
    path = tmp_path / "nested" / "dirs" / "firing_log.json"
    record_firing(
        firing_log_path=path,
        fired_at=_now_iso(),
        trigger_type="substantive_artifact",
        triggering_artifact={"source": "x:@leopoldasch"},
        mandatory_escalation=False,
        loop1_firing_id="L1-test",
    )
    assert path.exists()
    log = load_firing_log(path)
    assert len(log["firings"]) == 1
    assert log["firings"][0]["loop1_firing_id"] == "L1-test"


def test_record_firing_appends_not_overwrites(tmp_path: Path):
    path = tmp_path / "firing_log.json"
    for i in range(3):
        record_firing(
            firing_log_path=path,
            fired_at=_hours_ago(i),
            trigger_type="substantive_artifact",
            triggering_artifact=None,
            mandatory_escalation=False,
            loop1_firing_id=f"L1-{i}",
        )
    log = load_firing_log(path)
    assert len(log["firings"]) == 3
    assert {e["loop1_firing_id"] for e in log["firings"]} == {"L1-0", "L1-1", "L1-2"}


# ---------------------------------------------------------------------------
# classify_pipeline (end-to-end)
# ---------------------------------------------------------------------------


def test_pipeline_tier1_pre_filter_path_fires(tmp_path: Path):
    path = tmp_path / "firing_log.json"
    entry = classify_pipeline(
        _artifact(source="13f:sa_lp", snippet="new 13F"),
        firing_log_path=path,
        persist=True,
        loop1_firing_id="L1-firing-1",
    )
    out = entry.output
    assert out["classification"]["tier"] == 1
    assert out["rate_limit_decision"]["fired"] is True
    log = load_firing_log(path)
    assert len(log["firings"]) == 1


def test_pipeline_requires_llm_verdict_when_pre_filter_defers(tmp_path: Path):
    art = _artifact(source="x:@leopoldasch", snippet="A" * 200)
    with pytest.raises(ValueError, match="pre_filter could not decide"):
        classify_pipeline(art, firing_log_path=tmp_path / "log.json")


def test_pipeline_llm_verdict_path_persists(tmp_path: Path):
    # ≥150-char snippet from a listed handle → pre-filter defers to LLM
    art = _artifact(
        source="x:@leopoldasch",
        snippet=(
            "Detailed long post about SNDK fundamentals, the NAND cycle position, "
            "and our continued conviction in the AI-storage step-up. Sticking with "
            "the thesis despite Q2 prints."
        ),
    )
    llm_verdict = {
        "tier": 2,
        "reason": "ticker reference SNDK",
        "matched_rule": "tier_2_x_with_ticker",
    }
    path = tmp_path / "firing_log.json"
    entry = classify_pipeline(
        art,
        firing_log_path=path,
        llm_verdict=llm_verdict,
        persist=True,
        loop1_firing_id="L1-via-llm",
    )
    assert entry.output["classification"]["tier"] == 2
    assert entry.output["rate_limit_decision"]["fired"] is True
    log = load_firing_log(path)
    assert log["firings"][-1]["loop1_firing_id"] == "L1-via-llm"


def test_pipeline_does_not_persist_when_rate_limited(tmp_path: Path):
    """Cap consumed, no escalation → fire decision = False → no log entry."""
    path = tmp_path / "firing_log.json"
    # Seed the log with 3 firings in the past 7 days
    for i in range(3):
        record_firing(
            firing_log_path=path,
            fired_at=_hours_ago(i * 24 + 1),
            trigger_type="substantive_artifact",
            triggering_artifact=None,
            mandatory_escalation=False,
            loop1_firing_id=f"prior-{i}",
        )
    before = len(load_firing_log(path)["firings"])

    art = _artifact(source="13f:sa_lp", snippet="another filing")
    entry = classify_pipeline(art, firing_log_path=path, persist=True)
    # 13f always wants to fire — but in this case there's no mandatory escalation
    # signal AND no press-tour burst (3 firings spread over 3 days, not 24h).
    # So the cap kicks in.
    assert entry.output["rate_limit_decision"]["fired"] is False
    after = len(load_firing_log(path)["firings"])
    assert after == before  # no new entry


def test_pipeline_trace_entry_round_trippable(tmp_path: Path):
    entry = classify_pipeline(
        _artifact(source="13f:sa_lp", snippet="new"),
        firing_log_path=tmp_path / "log.json",
        persist=False,
    )
    json.dumps(entry.inputs)
    json.dumps(entry.output)
