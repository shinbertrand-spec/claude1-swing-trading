"""Tests for tools.observability.health_snapshot — the read-only data layer.

No network: feed probes are disabled via check_feeds=False. Run dirs are
synthesised in tmp_path, plus one read-only integration test against the real
2026-06-10 outage fixture committed to the repo.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tools.observability.health_snapshot import (
    build_snapshot,
    compute_silent_failure,
    find_latest_entry_session,
    cron_health,
)

UTC = timezone.utc
# 2026-06-10 is a Wednesday; 13:35 UTC = 9:35 EDT → inside the scheduled window.
SCHEDULED_START = "2026-06-10T13:35:11+00:00"
ADHOC_START = "2026-06-10T07:11:49+00:00"  # 03:11 EDT → NOT scheduled


def _write_run(runs_dir: Path, run_id: str, rows, *, started=SCHEDULED_START,
               errors=None):
    d = runs_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    results = [
        {"ticker": t, "status": s, "placed": s == "placed",
         "broker_order_id": oid, "ledger_path": None, "reason": None}
        for (t, s, oid) in rows
    ]
    n_placed = sum(1 for r in results if r["placed"])
    (d / "07_placement_results.yml").write_text(
        yaml.safe_dump({"results": results, "n_placed": n_placed}))
    (d / "_status.yml").write_text(yaml.safe_dump({
        "run_id": run_id,
        "run_started_at": started,
        "last_phase_completed": "post_panel",
        "errors": errors or [],
    }))
    return d


# ---- unprotected starter (FOLD-IN #1: naked held-recovery) --------------


def _write_paper_ledger(ledgers_dir: Path, ticker: str, *, state: str,
                        stop_order_id=None, stop_place_error=None):
    ledgers_dir.mkdir(parents=True, exist_ok=True)
    ps = {"stage": "STARTER", "starter": {"shares": 5}}
    if stop_order_id is not None:
        ps["stop_order_id"] = stop_order_id
    if stop_place_error is not None:
        ps["stop_place_error"] = stop_place_error
    (ledgers_dir / f"{ticker}.yml").write_text(yaml.safe_dump({
        "meta": {"ticker": ticker, "state": state, "account_track": "paper-auto"},
        "position_state": ps,
    }))


def test_unprotected_starter_is_surfaced(tmp_path):
    from tools.observability.health_snapshot import scan_unprotected_starters
    led = tmp_path / "ledgers" / "paper-auto"
    # naked: starter, no stop_order_id, stop placement errored
    _write_paper_ledger(led, "NAK", state="starter", stop_place_error="place_stop_loss failed")
    # healthy: starter WITH a live stop → must NOT be flagged
    _write_paper_ledger(led, "OK", state="starter", stop_order_id=12345)
    # closed: irrelevant
    _write_paper_ledger(led, "DONE", state="closed")
    found = scan_unprotected_starters(paper_auto_ledgers=led)
    tickers = {f["ticker"] for f in found}
    assert tickers == {"NAK"}


def test_naked_starter_not_held_does_not_page(tmp_path):
    """FIX #5: a starter still carrying a stop_place_error flag but NO LONGER
    held at the broker (closed externally before the flag was cleared) must NOT
    page — only a still-held starter is actually naked. Holdings gate the scan."""
    from tools.observability.health_snapshot import scan_unprotected_starters
    led = tmp_path / "ledgers" / "paper-auto"
    _write_paper_ledger(led, "NAK", state="starter", stop_place_error="x")
    _write_paper_ledger(led, "HELD", state="starter", stop_place_error="x")
    # Broker holds HELD (5 sh) but NOT NAK → only HELD is genuinely naked.
    found = scan_unprotected_starters(paper_auto_ledgers=led, holdings={"HELD": 5})
    assert {f["ticker"] for f in found} == {"HELD"}


def test_naked_starter_zero_qty_not_held(tmp_path):
    """A holdings snapshot that lists the symbol at 0 shares is not-held."""
    from tools.observability.health_snapshot import scan_unprotected_starters
    led = tmp_path / "ledgers" / "paper-auto"
    _write_paper_ledger(led, "NAK", state="starter", stop_place_error="x")
    found = scan_unprotected_starters(paper_auto_ledgers=led, holdings={"NAK": 0})
    assert found == []


def test_naked_starter_holdings_none_keeps_flag(tmp_path):
    """holdings=None (broker read unavailable) → conservative: still surface, so a
    blackout can never silence a genuine naked position."""
    from tools.observability.health_snapshot import scan_unprotected_starters
    led = tmp_path / "ledgers" / "paper-auto"
    _write_paper_ledger(led, "NAK", state="starter", stop_place_error="x")
    found = scan_unprotected_starters(paper_auto_ledgers=led, holdings=None)
    assert {f["ticker"] for f in found} == {"NAK"}


def test_unprotected_starter_fires_alarm(tmp_path):
    d = _write_run(tmp_path, "2026-06-10T13-35-11", [("AMD", "placed", 111)])
    pj = tmp_path / "journal" / "paper-auto" / "positions.json"
    pj.parent.mkdir(parents=True, exist_ok=True)
    pj.write_text(json.dumps({"positions": []}))
    led = tmp_path / "ledgers" / "paper-auto"
    _write_paper_ledger(led, "NAK", state="starter", stop_place_error="boom")
    snap = build_snapshot(run_dir=d, runs_dir=tmp_path, positions_json=pj,
                          paper_auto_ledgers=led, check_feeds=False, write=False)
    assert snap.silent_failure is not None
    assert any(f["ticker"] == "NAK" for f in snap.silent_failure.unprotected_starters)
    assert snap.silent_failure.alarm is True
    assert snap.overall_ok is False


# ---- orphan-intent alarm (v2 write-ahead path) --------------------------


def _write_intent(positions_json: Path, cloid: str, ticker: str, status: str,
                  broker_order_id=None):
    intents_dir = positions_json.parent / "intents"
    intents_dir.mkdir(parents=True, exist_ok=True)
    (intents_dir / f"{cloid}.yml").write_text(yaml.safe_dump({
        "client_order_id": cloid, "ticker": ticker, "status": status,
        "broker_order_id": broker_order_id, "created_at": "2026-06-10T13:35:00+00:00",
    }))


def test_dangling_placed_intent_fires_alarm(tmp_path):
    """A non-terminal write-ahead intent (status=placed) pages regardless of
    whether the run placed cleanly — an order intent is dangling."""
    d = _write_run(tmp_path, "2026-06-10T13-35-11", [("AMD", "placed", 111)])
    pj = tmp_path / "journal" / "paper-auto" / "positions.json"
    pj.parent.mkdir(parents=True, exist_ok=True)
    pj.write_text(json.dumps({"positions": [
        {"ticker": "AMD", "broker_order_id": 111, "stage": "starter"}]}))
    _write_intent(pj, "ap-XYZ-1", "XYZ", "placed", broker_order_id=999)
    sf = compute_silent_failure(d, positions_json=pj)
    assert sf.alarm is True
    assert len(sf.orphan_intents) == 1
    assert sf.orphan_intents[0]["ticker"] == "XYZ"
    assert "dangling" in sf.reason


def test_terminal_intents_do_not_alarm(tmp_path):
    """ledgered / abandoned intents are settled — no alarm."""
    d = _write_run(tmp_path, "2026-06-10T13-35-11", [("AMD", "placed", 111)])
    pj = tmp_path / "journal" / "paper-auto" / "positions.json"
    pj.parent.mkdir(parents=True, exist_ok=True)
    pj.write_text(json.dumps({"positions": [
        {"ticker": "AMD", "broker_order_id": 111, "stage": "starter"}]}))
    _write_intent(pj, "ap-AMD-1", "AMD", "ledgered", broker_order_id=111)
    _write_intent(pj, "ap-OLD-1", "OLD", "abandoned")
    sf = compute_silent_failure(d, positions_json=pj)
    assert sf.alarm is False
    assert sf.orphan_intents == []


# ---- silent-failure core ------------------------------------------------


def test_all_dry_run_scheduled_fires_alarm(tmp_path):
    d = _write_run(tmp_path, "2026-06-10T13-35-11", [
        ("AMD", "dry_run", None), ("CAT", "dry_run", None),
        ("MO", "defer", None), ("SBUX", "defer", None),
    ])
    sf = compute_silent_failure(d, positions_json=tmp_path / "nope.json")
    assert sf.intended == 2          # 2 dry_run; defer excluded
    assert sf.placed == 0
    assert sf.dry_run == 2
    assert sf.defer == 2
    assert sf.is_scheduled_entry is True
    assert sf.alarm is True


def test_rejected_and_defer_only_no_alarm(tmp_path):
    d = _write_run(tmp_path, "2026-06-10T13-35-11", [
        ("KLAC", "rejected", None), ("NUE", "rejected", None), ("MO", "defer", None),
    ])
    sf = compute_silent_failure(d, positions_json=tmp_path / "nope.json")
    assert sf.intended == 0
    assert sf.alarm is False
    assert "no candidates reached placement" in sf.reason


def test_all_placed_no_alarm(tmp_path):
    d = _write_run(tmp_path, "2026-06-10T13-35-11", [
        ("AMD", "placed", 111), ("CAT", "placed", 222),
    ])
    sf = compute_silent_failure(d, positions_json=tmp_path / "nope.json")
    assert sf.intended == 2
    assert sf.placed == 2
    assert sf.dry_run == 0
    assert sf.alarm is False


def test_partial_placed_with_dry_run_fires(tmp_path):
    """Even one dry-run among placed rows is anomalous on a scheduled run."""
    d = _write_run(tmp_path, "2026-06-10T13-35-11", [
        ("AMD", "placed", 111), ("CAT", "dry_run", None),
    ])
    sf = compute_silent_failure(d, positions_json=tmp_path / "nope.json")
    assert sf.placed == 1 and sf.dry_run == 1
    assert sf.alarm is True


def test_adhoc_dry_run_does_not_page(tmp_path):
    """A manual --dry-run run outside the entry window must NOT alarm."""
    d = _write_run(tmp_path, "2026-06-10T07-11-49", [
        ("AMD", "dry_run", None), ("CAT", "dry_run", None),
    ], started=ADHOC_START)
    sf = compute_silent_failure(d, positions_json=tmp_path / "nope.json")
    assert sf.dry_run == 2
    assert sf.is_scheduled_entry is False
    assert sf.alarm is False
    assert "not a scheduled entry run" in sf.reason


# ---- desync cross-check -------------------------------------------------


def test_desync_placed_not_at_broker(tmp_path):
    d = _write_run(tmp_path, "2026-06-10T13-35-11", [
        ("AMD", "placed", 111), ("CAT", "placed", 222),
    ])
    pj = tmp_path / "positions.json"
    pj.write_text(json.dumps({"positions": [
        {"ticker": "AMD", "broker_order_id": 111, "stage": "starter"},
        # CAT missing → desync
    ]}))
    sf = compute_silent_failure(d, positions_json=pj)
    tickers = [m["ticker"] for m in sf.placed_not_at_broker]
    assert tickers == ["CAT"]


def test_no_desync_when_all_present(tmp_path):
    d = _write_run(tmp_path, "2026-06-10T13-35-11", [("AMD", "placed", 111)])
    pj = tmp_path / "positions.json"
    pj.write_text(json.dumps({"positions": [
        {"ticker": "AMD", "broker_order_id": 111, "stage": "starter"}]}))
    sf = compute_silent_failure(d, positions_json=pj)
    assert sf.placed_not_at_broker == []


# ---- find_latest_entry_session -----------------------------------------


def test_find_latest_ignores_dirs_without_placement(tmp_path):
    _write_run(tmp_path, "2026-06-09T13-35-00", [("AMD", "placed", 1)])
    newest = _write_run(tmp_path, "2026-06-10T13-35-11", [("CAT", "placed", 2)])
    # a monitor-style dir without a placement file must be ignored
    (tmp_path / "2026-06-10T20-00-00").mkdir()
    found = find_latest_entry_session(tmp_path)
    assert found == newest


def test_find_latest_none_when_empty(tmp_path):
    assert find_latest_entry_session(tmp_path) is None


# ---- errors + build_snapshot composition --------------------------------


def test_build_snapshot_no_feeds_never_raises(tmp_path):
    d = _write_run(tmp_path, "2026-06-10T13-35-11", [("AMD", "dry_run", None)],
                   errors=[{"phase": "post_skeptic", "message": "boom"}])
    snap = build_snapshot(
        run_dir=d, runs_dir=tmp_path, positions_json=tmp_path / "p.json",
        paper_auto_ledgers=tmp_path / "ledgers", check_feeds=False,
        write=False, now_utc=datetime(2026, 6, 10, 14, 0, tzinfo=UTC),
    )
    assert snap.silent_failure.alarm is True
    assert snap.error_count == 1
    assert snap.feeds == []
    assert snap.overall_ok is False


def test_build_snapshot_writes_json(tmp_path):
    d = _write_run(tmp_path, "2026-06-10T13-35-11", [("AMD", "placed", 1)])
    out = tmp_path / "obs" / "snap.json"
    build_snapshot(run_dir=d, runs_dir=tmp_path, check_feeds=False,
                   write=True, snapshot_out=out,
                   now_utc=datetime(2026, 6, 10, 14, 0, tzinfo=UTC))
    assert out.is_file()
    doc = json.loads(out.read_text())
    assert doc["silent_failure"]["placed"] == 1


# ---- cron health --------------------------------------------------------


def test_entry_overdue_on_trading_day(tmp_path):
    # latest entry session was yesterday; now is 2026-06-10 11:00 ET (past 10:20)
    _write_run(tmp_path, "2026-06-09T13-35-00", [("AMD", "placed", 1)],
               started="2026-06-09T13:35:00+00:00")
    now = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)  # 11:00 EDT, Wed
    tasks = cron_health(now, runs_dir=tmp_path, paper_auto_ledgers=tmp_path / "x")
    entry = next(t for t in tasks if t.name == "AutoPaperEntry")
    assert entry.overdue is True


def test_entry_not_overdue_when_ran_today(tmp_path):
    _write_run(tmp_path, "2026-06-10T13-35-11", [("AMD", "placed", 1)])
    now = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
    tasks = cron_health(now, runs_dir=tmp_path, paper_auto_ledgers=tmp_path / "x")
    entry = next(t for t in tasks if t.name == "AutoPaperEntry")
    assert entry.overdue is False


# ---- read-only integration on the REAL outage fixture -------------------

_REAL_OUTAGE = Path(__file__).resolve().parents[1] / "ledgers" / "_auto_paper_runs" / "2026-06-10T13-35-11"


@pytest.mark.skipif(not (_REAL_OUTAGE / "07_placement_results.yml").is_file(),
                    reason="real outage fixture not present")
def test_real_outage_fixture_would_have_fired():
    sf = compute_silent_failure(_REAL_OUTAGE)
    assert sf.alarm is True
    assert sf.intended == 10
    assert sf.placed == 0
    assert sf.dry_run == 10
