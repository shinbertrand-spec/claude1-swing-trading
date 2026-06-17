"""Tests for tools.observability.health_html — static panel renderer."""
from __future__ import annotations

from pathlib import Path

from tools.observability.health_html import render, write_html
from tools.observability.health_snapshot import (
    CronTask,
    FeedStatus,
    HealthSnapshot,
    SilentFailure,
)


def _snap(alarm: bool):
    sf = SilentFailure(
        run_id="2026-06-10T13-35-11", run_dir="x",
        session_started_iso="2026-06-10T13:35:11+00:00", is_scheduled_entry=True,
        intended=10, placed=0 if alarm else 10, dry_run=10 if alarm else 0,
        errors=0, rejected=2, defer=4, n_total=16, alarm=alarm,
        reason="silent failure" if alarm else "nominal",
        placed_not_at_broker=[{"ticker": "CAT", "broker_order_id": None}] if alarm else [],
    )
    return HealthSnapshot(
        generated_at="2026-06-10T15:00:00+00:00", silent_failure=sf,
        cron_tasks=[CronTask("AutoPaperEntry", "2026-06-10T13:35:11+00:00", alarm, "detail")],
        feeds=[FeedStatus("EDGAR", True, "t", "reachable"),
               FeedStatus("BrokerAuth", not alarm, "t",
                          "paper acct ...1234 reachable" if not alarm else "unreachable")],
        error_count=0, overall_ok=not alarm,
    )


def test_render_alarm_shows_silent_failure():
    html = render(_snap(alarm=True))
    assert "SILENT FAILURE" in html
    assert "ATTENTION NEEDED" in html
    assert ">10<" in html          # intended/dry counts present
    assert "placed-but-not-at-broker" in html
    assert html.startswith("<!doctype html>")


def test_render_nominal():
    html = render(_snap(alarm=False))
    assert "NOMINAL" in html
    assert "ALL CLEAR" in html


def test_render_accepts_dict():
    html = render(_snap(alarm=True).to_dict())
    assert "Auto-Paper Health" in html


def test_render_no_secrets():
    html = render(_snap(alarm=False)).lower()
    assert "telegram_bot_token" not in html
    assert "private key" not in html
    # masked account ok; no long bare digit run
    import re
    assert not re.search(r"\d{7,}", html)


def test_write_html(tmp_path):
    out = tmp_path / "obs" / "health.html"
    p = write_html(_snap(alarm=True), out)
    assert p == out
    assert out.is_file()
    assert "SILENT FAILURE" in out.read_text(encoding="utf-8")


def test_render_handles_no_session():
    snap = HealthSnapshot(generated_at="t", silent_failure=None,
                          cron_tasks=[], feeds=[], error_count=0, overall_ok=True)
    html = render(snap)
    assert "No entry session found" in html
