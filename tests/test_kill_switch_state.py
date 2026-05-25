"""Tests for tools.thematic_portfolio.kill_switch.state — peak / heartbeat /
kill-event flag / events.jsonl I/O."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.thematic_portfolio.kill_switch import state


# --- peak -----------------------------------------------------------------


def test_load_peak_missing_returns_none(tmp_path):
    assert state.load_peak(tmp_path) is None


def test_update_peak_initialises_on_first_call(tmp_path):
    peak = state.update_peak(100_000.0, state_dir=tmp_path)
    assert peak.peak_value == 100_000.0
    assert peak.updated_at
    assert peak.epoch_started_at == peak.updated_at

    reloaded = state.load_peak(tmp_path)
    assert reloaded.peak_value == 100_000.0


def test_update_peak_raises_only_on_increase(tmp_path):
    state.update_peak(100_000.0, state_dir=tmp_path, now_iso="2026-01-01T00:00:00+00:00")
    # Drop — peak unchanged
    p = state.update_peak(80_000.0, state_dir=tmp_path, now_iso="2026-01-02T00:00:00+00:00")
    assert p.peak_value == 100_000.0
    assert p.updated_at == "2026-01-01T00:00:00+00:00"  # not bumped
    # Rise — peak updates, epoch preserved
    p2 = state.update_peak(120_000.0, state_dir=tmp_path, now_iso="2026-01-03T00:00:00+00:00")
    assert p2.peak_value == 120_000.0
    assert p2.updated_at == "2026-01-03T00:00:00+00:00"
    assert p2.epoch_started_at == "2026-01-01T00:00:00+00:00"


def test_reset_epoch_creates_new_baseline(tmp_path):
    state.update_peak(200_000.0, state_dir=tmp_path, now_iso="2026-01-01T00:00:00+00:00")
    reset = state.reset_epoch(50_000.0, state_dir=tmp_path, now_iso="2026-02-01T00:00:00+00:00")
    assert reset.peak_value == 50_000.0
    assert reset.epoch_started_at == "2026-02-01T00:00:00+00:00"


# --- heartbeat -------------------------------------------------------------


def test_heartbeat_round_trip(tmp_path):
    hb = state.HeartbeatState(
        last_cycle_at="2026-05-25T20:00:00+00:00",
        cycle_number=42,
        dry_run=True,
        last_action="hold",
        last_tier=0,
    )
    state.save_heartbeat(hb, state_dir=tmp_path)
    reloaded = state.load_heartbeat(tmp_path)
    assert reloaded.cycle_number == 42
    assert reloaded.last_action == "hold"
    assert reloaded.dry_run is True


def test_heartbeat_missing_returns_none(tmp_path):
    assert state.load_heartbeat(tmp_path) is None


# --- kill-event flag -------------------------------------------------------


def test_kill_event_default_false_when_file_missing(tmp_path):
    flag = state.load_kill_event(tmp_path)
    assert flag.fired is False
    assert flag.fired_at is None


def test_set_kill_event_persists_metadata(tmp_path):
    flag = state.set_kill_event(
        signal_type="thesis_abandonment",
        matched_phrase="we've sold our",
        source_artifact_url="https://x.com/leopoldasch/status/123",
        notes="Aschenbrenner X post 2026-06-01",
        state_dir=tmp_path,
        now_iso="2026-06-01T15:30:00+00:00",
    )
    assert flag.fired is True
    assert flag.signal_type == "thesis_abandonment"

    reloaded = state.load_kill_event(tmp_path)
    assert reloaded.fired is True
    assert reloaded.matched_phrase == "we've sold our"
    assert reloaded.fired_at == "2026-06-01T15:30:00+00:00"


def test_set_kill_event_is_idempotent_preserves_first_fire(tmp_path):
    state.set_kill_event(
        signal_type="thesis_abandonment",
        matched_phrase="we exited",
        state_dir=tmp_path,
        now_iso="2026-06-01T15:30:00+00:00",
    )
    second = state.set_kill_event(
        signal_type="sa_lp_event",
        matched_phrase="fund unwinding",
        state_dir=tmp_path,
        now_iso="2026-06-02T15:30:00+00:00",
    )
    # First-fire metadata preserved
    assert second.fired_at == "2026-06-01T15:30:00+00:00"
    assert second.signal_type == "thesis_abandonment"


def test_clear_kill_event_archives_and_removes(tmp_path):
    state.set_kill_event(
        signal_type="thesis_abandonment",
        matched_phrase="we exited",
        state_dir=tmp_path,
    )
    assert state.load_kill_event(tmp_path).fired is True

    state.clear_kill_event(state_dir=tmp_path, cleared_by="bertrand_manual_review")

    # Flag file gone
    assert not (tmp_path / state.KILL_EVENT_FILENAME).exists()
    # Default-empty reload
    assert state.load_kill_event(tmp_path).fired is False
    # Archive written
    archives = list(tmp_path.glob("aschenbrenner_kill_event_cleared_*.json"))
    assert len(archives) == 1
    archived = json.loads(archives[0].read_text(encoding="utf-8"))
    assert archived["cleared_by"] == "bertrand_manual_review"
    assert archived["originally"]["fired"] is True


def test_clear_kill_event_noop_when_unset(tmp_path):
    state.clear_kill_event(state_dir=tmp_path)
    assert not list(tmp_path.glob("aschenbrenner_kill_event_cleared_*.json"))


# --- events.jsonl ----------------------------------------------------------


def _mk_event(cycle_number=1, action="hold", tier=0, **kw):
    base = dict(
        cycle_id=f"cycle-{cycle_number:04d}",
        cycle_number=cycle_number,
        fired_at="2026-05-25T20:00:00+00:00",
        dry_run=True,
        action=action,
        tier=tier,
        drawdown_pct=0.0,
        current_allocation_pct=0.25,
        target_allocation_pct=0.25,
        sell_fraction=0.0,
        thematic_market_value=250_000.0,
        peak_thematic_value=250_000.0,
        total_account_value=1_000_000.0,
        aschenbrenner_kill_event=False,
        aschenbrenner_override=False,
        rationale="ok",
    )
    base.update(kw)
    return state.CycleEvent(**base)


def test_append_event_creates_file(tmp_path):
    state.append_event(_mk_event(), state_dir=tmp_path)
    p = tmp_path / state.EVENTS_FILENAME
    assert p.exists()
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    doc = json.loads(lines[0])
    assert doc["cycle_id"] == "cycle-0001"


def test_append_event_appends_in_order(tmp_path):
    state.append_event(_mk_event(1), state_dir=tmp_path)
    state.append_event(_mk_event(2, action="deleverage", tier=1), state_dir=tmp_path)
    state.append_event(_mk_event(3), state_dir=tmp_path)

    events = state.read_events(state_dir=tmp_path)
    assert [e["cycle_number"] for e in events] == [1, 2, 3]
    assert events[1]["tier"] == 1


def test_read_events_limit_returns_newest_first(tmp_path):
    for i in range(1, 6):
        state.append_event(_mk_event(i), state_dir=tmp_path)
    events = state.read_events(state_dir=tmp_path, limit=2)
    assert [e["cycle_number"] for e in events] == [5, 4]


def test_most_recent_fired_tier_returns_zero_when_empty(tmp_path):
    assert state.most_recent_fired_tier(state_dir=tmp_path) == 0


def test_most_recent_fired_tier_skips_holds(tmp_path):
    state.append_event(_mk_event(1, action="hold", tier=0), state_dir=tmp_path)
    state.append_event(_mk_event(2, action="deleverage", tier=1), state_dir=tmp_path)
    state.append_event(_mk_event(3, action="hold", tier=0), state_dir=tmp_path)
    assert state.most_recent_fired_tier(state_dir=tmp_path) == 1


def test_most_recent_fired_tier_returns_latest(tmp_path):
    state.append_event(_mk_event(1, action="deleverage", tier=1), state_dir=tmp_path)
    state.append_event(_mk_event(2, action="deleverage", tier=2), state_dir=tmp_path)
    state.append_event(_mk_event(3, action="unwind", tier=3), state_dir=tmp_path)
    assert state.most_recent_fired_tier(state_dir=tmp_path) == 3
