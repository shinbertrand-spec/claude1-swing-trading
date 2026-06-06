"""Tests for the post-RTH stuck-closing reconciler (Mode A) + orphan discovery
(Mode B): tools.auto_paper.reconcile.reconcile_stuck_closing + the run_entry
cron-gate halt.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tools.auto_paper import cron_gate, critic_panel, reconcile, run_entry, state

# Reuse the broker fake from the reconcile test module.
from tests.test_auto_paper_reconcile import FakeTradeClient


def _client(*, open_=None):
    from tools.broker.tiger import TigerClient
    return TigerClient(_trade_client=FakeTradeClient(open_=open_ or []))


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    ledger_dir = tmp_path / "ledgers" / "paper-auto"
    positions_json = tmp_path / "journal" / "paper-auto" / "positions.json"
    gate = tmp_path / "journal" / "paper-auto" / "cron_gate.json"
    monkeypatch.setattr(state, "PAPER_AUTO_LEDGER_DIR", str(ledger_dir))
    monkeypatch.setattr(state, "PAPER_AUTO_POSITIONS_JSON", str(positions_json))
    monkeypatch.setattr(cron_gate, "GATE_PATH", str(gate))
    monkeypatch.setattr(critic_panel, "_PANEL_LEDGER_DIR", tmp_path / "swing-critics")
    return ledger_dir, positions_json, gate


def _seed(ticker, meta_state, *, shares=100, stop=90.0, no_stop=False):
    """Write a schema-valid ledger then set meta.state to the desired value."""
    state.write_submitted_ledger(
        ticker=ticker, setup_type="EP", setup_grade="Swan",
        pivot_price=100.0, limit_price=100.0, stop_price=stop,
        shares=shares, broker_order_id=111, broker="tiger_paper", sector_etf="XLK",
    )
    doc = state.load_ledger(ticker)
    doc["meta"]["state"] = meta_state
    if no_stop:
        doc["position_state"].pop("current_stop", None)
        doc["setup_classification"].pop("stop_price", None)
    with open(state.ledger_path(ticker), "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)


def _meta_state(ticker):
    return state.load_ledger(ticker)["meta"]["state"]


def _stp_sell_calls(client):
    return [c for c in client._tc.calls
            if c[0] == "place_order" and c[1] == "SELL"]


# ---- shape (a): closed-ledger-still-held flip-back ----

def test_closed_held_flips_to_starter(dirs):
    _seed("MXL", "closed", shares=503, stop=85.0)
    client = _client(open_=[])
    res = reconcile.reconcile_stuck_closing(client=client, holdings={"MXL": 503}, dry_run=False)
    assert [r.action for r in res] == ["reverted_to_starter"]
    assert _meta_state("MXL") == "starter"
    # positions.json now lists MXL as starter
    data = json.loads(Path(state.PAPER_AUTO_POSITIONS_JSON).read_text())
    mxl = [p for p in data["positions"] if p["ticker"] == "MXL"]
    assert mxl and mxl[0]["stage"] == "starter" and mxl[0]["shares"] == 503


# ---- shape (b): pending_close-ledger-still-held flip-back ----

def test_pending_close_held_flips_to_starter(dirs):
    _seed("COIN", "pending_close", shares=213, stop=150.0)
    client = _client(open_=[])
    res = reconcile.reconcile_stuck_closing(client=client, holdings={"COIN": 213}, dry_run=False)
    assert [r.action for r in res] == ["reverted_to_starter"]
    assert _meta_state("COIN") == "starter"


# ---- shape (c): no-resting-stop re-place ----

def test_no_resting_stop_replaces(dirs):
    _seed("MXL", "closed", shares=503, stop=85.0)
    client = _client(open_=[])           # no open orders => no live stop
    res = reconcile.reconcile_stuck_closing(client=client, holdings={"MXL": 503}, dry_run=False)
    assert res[0].stop_order_id is not None
    assert res[0].stop_place_error is None
    sells = _stp_sell_calls(client)
    assert len(sells) == 1
    assert sells[0][3] == "MXL"          # symbol
    assert sells[0][2] == 503            # qty sized to holding


def test_existing_live_stop_not_duplicated(dirs):
    from types import SimpleNamespace
    _seed("MXL", "closed", shares=503, stop=85.0)
    live_stp = SimpleNamespace(
        id=99001, contract=SimpleNamespace(symbol="MXL"), action="SELL",
        order_type="STP", quantity=503, limit_price=None, status="Submitted",
    )
    client = _client(open_=[live_stp])
    res = reconcile.reconcile_stuck_closing(client=client, holdings={"MXL": 503}, dry_run=False)
    assert res[0].action == "reverted_to_starter"
    assert _stp_sell_calls(client) == []   # did NOT place a duplicate stop


def test_no_stop_price_flags_for_operator(dirs):
    _seed("MXL", "closed", shares=503, no_stop=True)
    client = _client(open_=[])
    res = reconcile.reconcile_stuck_closing(client=client, holdings={"MXL": 503}, dry_run=False)
    assert res[0].action == "reverted_to_starter"
    assert res[0].stop_order_id is None
    assert "operator review" in (res[0].stop_place_error or "")
    assert _stp_sell_calls(client) == []


# ---- shape (d): orphan discovery + cron gating ----

def test_orphan_discovery_alerts_and_gates(dirs):
    _ledger_dir, _pos, gate = dirs
    _seed("GO", "starter", shares=100, stop=8.0)   # healthy, has ledger
    client = _client(open_=[])
    res = reconcile.reconcile_stuck_closing(
        client=client, holdings={"GO": 100, "GKOS": 338}, dry_run=False,
    )
    actions = {r.ticker: r.action for r in res}
    assert actions.get("GKOS") == "orphan_discovered"
    assert "GO" not in actions                       # healthy starter => no action row
    # cron gate set
    gated, doc = cron_gate.is_gated()
    assert gated is True
    assert "GKOS" in doc["payload"]["orphans"]
    # discovery file written
    disc = list(Path(state.PAPER_AUTO_POSITIONS_JSON).parent.glob("orphan_discovery_*.yml"))
    assert len(disc) == 1
    assert "GKOS" in yaml.safe_load(disc[0].read_text())["orphans"]


def test_phase_init_halts_when_gated(dirs, tmp_path, capsys):
    cron_gate.set_gate("orphan_discovery", {"orphans": ["GKOS"]})
    run_dir = tmp_path / "run"
    rc = run_entry.phase_init(run_dir)
    assert rc == 2
    assert "PHASE_INIT_GATED" in capsys.readouterr().out


# ---- dry-run + healthy + corrupt ----

def test_dry_run_mutates_nothing(dirs):
    _seed("MXL", "closed", shares=503, stop=85.0)
    client = _client(open_=[])
    res = reconcile.reconcile_stuck_closing(
        client=client, holdings={"MXL": 503, "ZZZ": 10}, dry_run=True,
    )
    actions = {r.ticker: r.action for r in res}
    assert actions["MXL"] == "stuck_dry_run"
    assert actions["ZZZ"] == "orphan_dry_run"
    assert _meta_state("MXL") == "closed"            # unchanged
    assert cron_gate.is_gated()[0] is False          # no gate set
    assert _stp_sell_calls(client) == []


def test_healthy_starter_no_action(dirs):
    _seed("GO", "starter", shares=100, stop=8.0)
    client = _client(open_=[])
    res = reconcile.reconcile_stuck_closing(client=client, holdings={"GO": 100}, dry_run=False)
    assert res == []                                  # nothing to do


def test_corrupt_held_surfaced_not_orphaned(dirs):
    ledger_dir, _pos, _gate = dirs
    ledger_dir.mkdir(parents=True, exist_ok=True)
    # broken YAML (bare sequence after a mapping key)
    (ledger_dir / "BAD.yml").write_text(
        "meta:\n  state: starter\nnotes: >\n  hi\n- id: 1\n  x: 2\n", encoding="utf-8")
    client = _client(open_=[])
    res = reconcile.reconcile_stuck_closing(client=client, holdings={"BAD": 50}, dry_run=False)
    assert [r.action for r in res] == ["corrupt_ledger"]
    assert cron_gate.is_gated()[0] is False           # corrupt != orphan; no gate
