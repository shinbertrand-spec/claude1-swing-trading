"""Tests for the artifact_classifier -> kill-switch flag wiring.

Verifies that ``classify_pipeline(persist=True)`` writes to the
``aschenbrenner_kill_event.json`` flag file when a mandatory-escalation
signal matches one of :data:`KILL_EVENT_SIGNAL_TYPES`. The flag is
read by Process B (kill-switch monitor) to trigger a Tier 3 full-unwind
regardless of drawdown.

Existing classifier tests in test_thematic_portfolio_artifact_classifier.py
cover the deterministic signal detection itself; these tests cover only
the new wiring path.
"""
from __future__ import annotations

from datetime import datetime, timezone

from tools.thematic_portfolio.artifact_classifier import (
    KILL_EVENT_SIGNAL_TYPES,
    ArtifactInput,
    classify_pipeline,
    extract_kill_event_signal,
)
from tools.thematic_portfolio.kill_switch import state


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _artifact(source, snippet, **kw):
    return ArtifactInput(
        source=source,
        snippet=snippet,
        snippet_length_chars=len(snippet),
        fetched_at=kw.pop("fetched_at", None) or _now_iso(),
        url=kw.pop("url", None),
        is_thread=kw.pop("is_thread", False),
        thread_total_words=kw.pop("thread_total_words", 0),
        author_handle=kw.pop("author_handle", None),
    )


# --- extract_kill_event_signal ---------------------------------------------


def test_extract_kill_event_signal_returns_first_match():
    sigs = [
        {"signal_type": "strategy_pivot", "matched_phrase": "we've pivoted"},
        {"signal_type": "thesis_abandonment", "matched_phrase": "we exited"},
    ]
    out = extract_kill_event_signal(sigs)
    assert out["signal_type"] == "thesis_abandonment"


def test_extract_kill_event_signal_returns_none_when_no_match():
    sigs = [{"signal_type": "strategy_pivot", "matched_phrase": "we've pivoted"}]
    assert extract_kill_event_signal(sigs) is None


def test_kill_event_signal_types_includes_all_design_categories():
    # Per [[swing-thematic-portfolio-kill-switch-architecture]]
    # § Aschenbrenner-specific kill events
    expected = {
        "thesis_abandonment",
        "sa_lp_event",
        "sa_lp_closure",
        "regulatory_action",
        "principal_incident",
    }
    assert expected <= set(KILL_EVENT_SIGNAL_TYPES)


# --- pipeline integration --------------------------------------------------


def test_thesis_abandonment_x_post_sets_kill_flag(tmp_path):
    """Tier 1 13F + 'we exited' phrase -> kill flag set."""
    firing_log = tmp_path / "firing_log.json"
    state_dir = tmp_path / "kill_state"
    artifact = _artifact(
        source="13f:sa_lp",
        snippet="SA LP filing. we exited our entire long book this quarter.",
        url="https://www.sec.gov/example.htm",
    )
    classify_pipeline(
        artifact,
        firing_log_path=firing_log,
        persist=True,
        kill_switch_state_dir=state_dir,
    )
    flag = state.load_kill_event(state_dir)
    assert flag.fired is True
    assert flag.signal_type == "thesis_abandonment"
    assert "we exited" in flag.matched_phrase
    assert flag.source_artifact_url == "https://www.sec.gov/example.htm"


def test_sa_lp_closure_phrase_sets_kill_flag(tmp_path):
    firing_log = tmp_path / "firing_log.json"
    state_dir = tmp_path / "kill_state"
    artifact = _artifact(
        source="press:fortune",
        url="https://fortune.com/sa-lp",
        snippet="Aschenbrenner announces SA LP closure effective Q3.",
    )
    classify_pipeline(
        artifact,
        firing_log_path=firing_log,
        persist=True,
        kill_switch_state_dir=state_dir,
    )
    flag = state.load_kill_event(state_dir)
    assert flag.fired is True
    assert flag.signal_type == "sa_lp_event"


def test_strategy_pivot_does_not_set_kill_flag(tmp_path):
    """Strategy pivot is mandatory-escalation but NOT a kill event."""
    firing_log = tmp_path / "firing_log.json"
    state_dir = tmp_path / "kill_state"
    artifact = _artifact(
        source="essay:forourposterity",
        url="https://forourposterity.com/p1",
        snippet="Detailed essay; we've pivoted from chip-bear to power-long.",
    )
    classify_pipeline(
        artifact,
        firing_log_path=firing_log,
        persist=True,
        kill_switch_state_dir=state_dir,
    )
    flag = state.load_kill_event(state_dir)
    assert flag.fired is False  # strategy_pivot != kill event


def test_persist_false_does_not_set_kill_flag(tmp_path):
    """Even with a kill-event signal, persist=False must not write."""
    firing_log = tmp_path / "firing_log.json"
    state_dir = tmp_path / "kill_state"
    artifact = _artifact(
        source="13f:sa_lp",
        snippet="SA LP filing. we exited entirely.",
    )
    classify_pipeline(
        artifact,
        firing_log_path=firing_log,
        persist=False,
        kill_switch_state_dir=state_dir,
    )
    flag = state.load_kill_event(state_dir)
    assert flag.fired is False


def test_kill_flag_is_first_fire_wins(tmp_path):
    """Two kill-event artifacts in sequence -> flag preserves first-fire metadata."""
    firing_log = tmp_path / "firing_log.json"
    state_dir = tmp_path / "kill_state"
    artifact1 = _artifact(
        source="13f:sa_lp",
        snippet="we exited the entire portfolio.",
        url="https://sec.gov/1",
        fetched_at="2026-06-01T15:30:00+00:00",
    )
    artifact2 = _artifact(
        source="press:fortune",
        url="https://fortune.com/2",
        snippet="fund unwinding confirmed.",
        fetched_at="2026-06-02T10:00:00+00:00",
    )
    classify_pipeline(
        artifact1,
        firing_log_path=firing_log,
        persist=True,
        kill_switch_state_dir=state_dir,
    )
    classify_pipeline(
        artifact2,
        firing_log_path=firing_log,
        persist=True,
        kill_switch_state_dir=state_dir,
    )
    flag = state.load_kill_event(state_dir)
    assert flag.fired_at == "2026-06-01T15:30:00+00:00"
    assert flag.signal_type == "thesis_abandonment"  # first wins
    assert flag.source_artifact_url == "https://sec.gov/1"


def test_pipeline_trace_entry_reports_kill_flag_outcome(tmp_path):
    firing_log = tmp_path / "firing_log.json"
    state_dir = tmp_path / "kill_state"
    artifact = _artifact(
        source="13f:sa_lp",
        snippet="we exited.",
    )
    entry = classify_pipeline(
        artifact,
        firing_log_path=firing_log,
        persist=True,
        kill_switch_state_dir=state_dir,
    )
    assert entry.output["kill_event_flag_set"] is not None
    assert entry.output["kill_event_flag_set"]["fired"] is True
    assert entry.output["kill_event_flag_set"]["signal_type"] == "thesis_abandonment"


def test_pipeline_trace_entry_reports_none_when_no_kill_signal(tmp_path):
    firing_log = tmp_path / "firing_log.json"
    state_dir = tmp_path / "kill_state"
    artifact = _artifact(
        source="13f:sa_lp",
        snippet="Routine quarterly filing with no significant changes.",
    )
    entry = classify_pipeline(
        artifact,
        firing_log_path=firing_log,
        persist=True,
        kill_switch_state_dir=state_dir,
    )
    assert entry.output["kill_event_flag_set"] is None
