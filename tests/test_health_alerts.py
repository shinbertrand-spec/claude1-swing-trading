"""Tests for tools.observability.health_alerts — edge-triggered dispatcher.

The Telegram send function is injected as a fake; no network. Asserts de-dup,
recovery, PII-safety, and the never-raise contract.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from tools.observability.health_alerts import dispatch_alerts
from tools.observability.health_snapshot import (
    CronTask,
    FeedStatus,
    HealthSnapshot,
    SilentFailure,
)

UTC = timezone.utc
NOW = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)


def _fake_send(captured):
    def send(message):
        captured.append(message)
        return SimpleNamespace(ok=True, error=None)
    return send


def _snap(*, sf=None, crons=None, feeds=None, errors=0):
    return HealthSnapshot(
        generated_at="2026-06-10T15:00:00+00:00",
        silent_failure=sf, cron_tasks=crons or [], feeds=feeds or [],
        error_count=errors, overall_ok=(sf is None and not errors),
    )


def _alarm_sf(run_id="2026-06-10T13-35-11"):
    return SilentFailure(
        run_id=run_id, run_dir="x", session_started_iso="2026-06-10T13:35:11+00:00",
        is_scheduled_entry=True, intended=10, placed=0, dry_run=10, errors=0,
        rejected=0, defer=4, n_total=14, alarm=True, reason="silent failure",
    )


def _orphan_sf(run_id="2026-06-10T13-35-11", orphans=None):
    return SilentFailure(
        run_id=run_id, run_dir="x", session_started_iso="2026-06-10T13:35:11+00:00",
        is_scheduled_entry=False, intended=0, placed=0, dry_run=0, errors=0,
        rejected=0, defer=0, n_total=0, alarm=True, reason="orphan intent",
        orphan_intents=orphans if orphans is not None else [
            {"client_order_id": "ap-NVDA-r1", "ticker": "NVDA", "status": "placed"}],
    )


# ---- orphan intents (FIX 3: re-page on identity + backoff, not run_id) ---


def test_orphan_repages_within_same_run_id_after_backoff(tmp_path):
    """A persistent orphan keeps the SAME run_id across the day's checks. The
    run_id-keyed silent-failure path would suppress it — the orphan path must
    re-page once the backoff elapses."""
    from datetime import timedelta
    from tools.observability import health_alerts as ha
    cap = []
    state = tmp_path / "state.json"
    snap = _snap(sf=_orphan_sf())
    r1 = dispatch_alerts(snap, state_path=state, send=_fake_send(cap), now_utc=NOW)
    assert "safety_page" in r1.sent
    # same identity, shortly after → suppressed (backoff not elapsed)
    r2 = dispatch_alerts(snap, state_path=state, send=_fake_send(cap),
                         now_utc=NOW + timedelta(minutes=30))
    assert "safety_page" in r2.suppressed
    # backoff elapsed → re-pages despite identical run_id
    later = NOW + timedelta(seconds=ha.ORPHAN_REPAGE_SECONDS + 60)
    r3 = dispatch_alerts(snap, state_path=state, send=_fake_send(cap), now_utc=later)
    assert "safety_page" in r3.sent
    assert len(cap) == 2


def test_orphan_repages_immediately_on_identity_change(tmp_path):
    from datetime import timedelta
    cap = []
    state = tmp_path / "state.json"
    dispatch_alerts(_snap(sf=_orphan_sf(orphans=[{"client_order_id": "a", "ticker": "AAA"}])),
                    state_path=state, send=_fake_send(cap), now_utc=NOW)
    # a DIFFERENT orphan set → re-page right away (no backoff wait)
    dispatch_alerts(_snap(sf=_orphan_sf(orphans=[{"client_order_id": "b", "ticker": "BBB"}])),
                    state_path=state, send=_fake_send(cap), now_utc=NOW + timedelta(minutes=5))
    assert len(cap) == 2


def test_orphan_clear_resets_then_repages(tmp_path):
    from datetime import timedelta
    cap = []
    state = tmp_path / "state.json"
    dispatch_alerts(_snap(sf=_orphan_sf()), state_path=state, send=_fake_send(cap), now_utc=NOW)
    # orphan resolved (no orphan_intents) → no page, state reset
    clean = _snap(sf=SilentFailure(
        run_id="r", run_dir="x", session_started_iso=None, is_scheduled_entry=False,
        intended=0, placed=0, dry_run=0, errors=0, rejected=0, defer=0, n_total=0,
        alarm=False, reason="ok"))
    dispatch_alerts(clean, state_path=state, send=_fake_send(cap),
                    now_utc=NOW + timedelta(minutes=10))
    # a new orphan appears → pages immediately (state was reset)
    dispatch_alerts(_snap(sf=_orphan_sf()), state_path=state, send=_fake_send(cap),
                    now_utc=NOW + timedelta(minutes=20))
    assert len(cap) == 2


# ---- silent failure -----------------------------------------------------


def test_silent_failure_alerts_once(tmp_path):
    cap = []
    state = tmp_path / "state.json"
    snap = _snap(sf=_alarm_sf())
    r1 = dispatch_alerts(snap, state_path=state, send=_fake_send(cap), now_utc=NOW)
    assert "silent_failure" in r1.sent
    assert any("ANOMALY" in m for m in cap)
    # second run, same run_id → suppressed (edge-triggered)
    r2 = dispatch_alerts(snap, state_path=state, send=_fake_send(cap), now_utc=NOW)
    assert "silent_failure" in r2.suppressed
    assert len(cap) == 1


def test_new_run_id_re_alerts(tmp_path):
    cap = []
    state = tmp_path / "state.json"
    dispatch_alerts(_snap(sf=_alarm_sf("day1")), state_path=state, send=_fake_send(cap), now_utc=NOW)
    dispatch_alerts(_snap(sf=_alarm_sf("day2")), state_path=state, send=_fake_send(cap), now_utc=NOW)
    assert len(cap) == 2  # each day's failed session pages once


def test_no_alarm_no_send(tmp_path):
    cap = []
    sf = _alarm_sf()
    sf.alarm = False
    r = dispatch_alerts(_snap(sf=sf), state_path=tmp_path / "s.json",
                        send=_fake_send(cap), now_utc=NOW)
    assert cap == []
    assert r.sent == []


# ---- feeds: down + recovery --------------------------------------------


def test_feed_down_then_recovered(tmp_path):
    cap = []
    state = tmp_path / "s.json"
    down = _snap(feeds=[FeedStatus("EDGAR", False, None, "unreachable"),
                        FeedStatus("PriceFeed", True, "t", "ok")])
    r1 = dispatch_alerts(down, state_path=state, send=_fake_send(cap), now_utc=NOW)
    assert "feed_down:EDGAR" in r1.sent
    # still down next run → suppressed
    r2 = dispatch_alerts(down, state_path=state, send=_fake_send(cap), now_utc=NOW)
    assert "feed_down:EDGAR" in r2.suppressed
    # recovered
    up = _snap(feeds=[FeedStatus("EDGAR", True, "t", "reachable"),
                      FeedStatus("PriceFeed", True, "t", "ok")])
    r3 = dispatch_alerts(up, state_path=state, send=_fake_send(cap), now_utc=NOW)
    assert "feed_recovered:EDGAR" in r3.sent
    assert any("RECOVERED" in m for m in cap)


# ---- entry overdue ------------------------------------------------------


def test_entry_overdue_alerts_once_per_day(tmp_path):
    cap = []
    state = tmp_path / "s.json"
    snap = _snap(crons=[CronTask("AutoPaperEntry", None, True, "OVERDUE")])
    r1 = dispatch_alerts(snap, state_path=state, send=_fake_send(cap), now_utc=NOW)
    assert "entry_overdue" in r1.sent
    r2 = dispatch_alerts(snap, state_path=state, send=_fake_send(cap), now_utc=NOW)
    assert "entry_overdue" in r2.suppressed


# ---- run errors ---------------------------------------------------------


def test_run_errors_alert(tmp_path):
    cap = []
    snap = _snap(sf=_alarm_sf(), errors=3)
    r = dispatch_alerts(snap, state_path=tmp_path / "s.json",
                        send=_fake_send(cap), now_utc=NOW)
    assert "run_errors" in r.sent
    assert any("error(s)" in m for m in cap)


# ---- PII-safety + never-raise ------------------------------------------


def test_messages_carry_no_secrets(tmp_path):
    cap = []
    snap = _snap(sf=_alarm_sf(),
                 feeds=[FeedStatus("BrokerAuth", True, "t", "paper acct ...1234 reachable")])
    dispatch_alerts(snap, state_path=tmp_path / "s.json",
                    send=_fake_send(cap), now_utc=NOW)
    blob = "\n".join(cap).lower()
    assert "telegram_bot_token" not in blob
    assert "private" not in blob and "secret" not in blob
    # no long bare digit runs (would suggest an unmasked account/token)
    import re
    assert not re.search(r"\d{7,}", "\n".join(m for m in cap if "run_id" not in m.lower()) or "")


def test_send_that_raises_is_caught(tmp_path):
    def boom(message):
        raise RuntimeError("network down")
    r = dispatch_alerts(_snap(sf=_alarm_sf()), state_path=tmp_path / "s.json",
                        send=boom, now_utc=NOW)
    assert any("silent_failure" in e for e in r.errors)
    # did not raise


def test_send_not_ok_recorded_as_error(tmp_path):
    def notok(message):
        return SimpleNamespace(ok=False, error="config_missing_token")
    r = dispatch_alerts(_snap(sf=_alarm_sf()), state_path=tmp_path / "s.json",
                        send=notok, now_utc=NOW)
    assert any("config_missing_token" in e for e in r.errors)
