"""Tests for the pre-session orphan sweep (Priority 2 — Mode-B defense-in-depth):
tools.auto_paper.reconcile.presession_sweep + its wiring into run_entry.phase_init.

The post-RTH reconciler (reconcile_stuck_closing) already discovers orphans and
sets the gate at market close. This sweep is the FRESH morning check that runs
even when the post-RTH reconciler never ran, so the entry self-protects before
placing. It is read-only w.r.t. broker + ledgers: it never flips, places, or
closes — its only side effects are persisting a discovery file and setting the
cron gate.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tools.auto_paper import cron_gate, critic_panel, reconcile, run_entry, state


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


def _seed(ticker, meta_state, *, shares=100, stop=90.0):
    state.write_submitted_ledger(
        ticker=ticker, setup_type="EP", setup_grade="Swan",
        pivot_price=100.0, limit_price=100.0, stop_price=stop,
        shares=shares, broker_order_id=111, broker="tiger_paper", sector_etf="XLK",
    )
    doc = state.load_ledger(ticker)
    doc["meta"]["state"] = meta_state
    with open(state.ledger_path(ticker), "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)


def _discovery_files():
    return list(Path(state.PAPER_AUTO_POSITIONS_JSON).parent.glob("orphan_discovery_*.yml"))


# ---- clean: healthy starter only ----

def test_clean_book_no_gate(dirs):
    _seed("GO", "starter", shares=100, stop=8.0)
    sweep = reconcile.presession_sweep(holdings={"GO": 100})
    assert sweep.gated_now is False
    assert sweep.orphans == [] and sweep.corrupt_held == []
    assert sweep.healthy == ["GO"]
    assert cron_gate.is_gated()[0] is False
    assert _discovery_files() == []


# ---- true orphan (no ledger) gates ----

def test_true_orphan_sets_gate_and_persists(dirs):
    _seed("GO", "starter", shares=100, stop=8.0)
    sweep = reconcile.presession_sweep(holdings={"GO": 100, "GKOS": 338})
    assert sweep.gated_now is True
    assert sweep.orphans == ["GKOS"]
    gated, doc = cron_gate.is_gated()
    assert gated is True
    assert doc["reason"] == "presession_orphan_sweep"
    assert "GKOS" in doc["payload"]["orphans"]
    files = _discovery_files()
    assert len(files) == 1
    disc = yaml.safe_load(files[0].read_text())
    assert disc["source"] == "presession_sweep"
    assert "GKOS" in disc["orphans"]


def test_orphan_sweep_never_mutates_ledgers(dirs):
    """Read-only: a healthy starter alongside an orphan is left untouched."""
    _seed("GO", "starter", shares=100, stop=8.0)
    reconcile.presession_sweep(holdings={"GO": 100, "ZZ": 5})
    assert state.load_ledger("GO")["meta"]["state"] == "starter"  # unchanged


# ---- corrupt-held gates (divergence from the post-RTH reconciler) ----

def test_corrupt_held_gates(dirs):
    ledger_dir, _pos, _gate = dirs
    ledger_dir.mkdir(parents=True, exist_ok=True)
    (ledger_dir / "BAD.yml").write_text(
        "meta:\n  state: starter\nnotes: >\n  hi\n- id: 1\n  x: 2\n", encoding="utf-8")
    sweep = reconcile.presession_sweep(holdings={"BAD": 50})
    assert sweep.corrupt_held == ["BAD"]
    assert sweep.gated_now is True
    gated, doc = cron_gate.is_gated()
    assert gated is True
    assert "BAD" in doc["payload"]["corrupt_held"]
    disc = yaml.safe_load(_discovery_files()[0].read_text())
    assert "BAD" in disc["corrupt_held"]


# ---- stuck_closing is the reconciler's domain: surfaced, NOT gated ----

def test_stuck_closing_surfaced_not_gated(dirs):
    _seed("MXL", "closed", shares=503, stop=85.0)   # held + closed ledger
    sweep = reconcile.presession_sweep(holdings={"MXL": 503})
    assert sweep.stuck_closing == ["MXL"]
    assert sweep.orphans == [] and sweep.corrupt_held == []
    assert sweep.gated_now is False
    assert cron_gate.is_gated()[0] is False


# ---- dry-run detects but does not gate / persist ----

def test_dry_run_detects_without_side_effects(dirs):
    sweep = reconcile.presession_sweep(holdings={"ZZ": 10}, dry_run=True)
    assert sweep.orphans == ["ZZ"]
    assert sweep.gated_now is False
    assert sweep.discovery_path is None
    assert cron_gate.is_gated()[0] is False
    assert _discovery_files() == []


# ---- broker fetch failure is non-fatal (skipped) ----

def test_broker_fetch_failure_is_skipped(dirs):
    from tools.broker.tiger import BrokerOrderError

    def _boom():
        raise BrokerOrderError("positions endpoint down")

    client = SimpleNamespace(positions=_boom)
    sweep = reconcile.presession_sweep(client=client)
    assert sweep.skipped is True
    assert "down" in (sweep.skip_reason or "")
    assert sweep.gated_now is False
    assert cron_gate.is_gated()[0] is False


def test_requires_client_or_holdings(dirs):
    with pytest.raises(ValueError, match="client.*holdings"):
        reconcile.presession_sweep()


def test_gate_tickers_property(dirs):
    sweep = reconcile.PresessionSweep(
        holdings={}, healthy=[], orphans=["B", "A"], corrupt_held=["C"],
        stuck_closing=[], submitted_held=[], gated_now=True, discovery_path=None,
    )
    assert sweep.gate_tickers == ["A", "B", "C"]


# ---- client path: positions() fetched + classified ----

def test_client_positions_fetched_and_classified(dirs):
    _seed("GO", "starter", shares=100, stop=8.0)
    client = SimpleNamespace(
        positions=lambda: SimpleNamespace(
            output={"positions": [
                {"symbol": "GO", "quantity": 100},
                {"symbol": "GKOS", "quantity": 338},
            ]}
        )
    )
    sweep = reconcile.presession_sweep(client=client)
    assert sweep.healthy == ["GO"]
    assert sweep.orphans == ["GKOS"]
    assert sweep.gated_now is True


# ---- phase_init wiring: fresh orphan halts entry with NO prior gate set ----

def test_phase_init_gated_by_fresh_sweep(tmp_path, monkeypatch, capsys):
    """No prior gate, but the broker holds an unledgered orphan -> phase_init
    halts via the fresh pre-session sweep (not the stale-gate fast-path)."""
    ledger_dir = tmp_path / "ledgers" / "paper-auto"      # empty -> ZZTEST is orphan
    ledger_dir.mkdir(parents=True, exist_ok=True)
    gate = tmp_path / "journal" / "paper-auto" / "cron_gate.json"
    pos_json = tmp_path / "journal" / "paper-auto" / "positions.json"
    monkeypatch.setattr(state, "PAPER_AUTO_LEDGER_DIR", str(ledger_dir))
    monkeypatch.setattr(state, "PAPER_AUTO_POSITIONS_JSON", str(pos_json))
    monkeypatch.setattr(cron_gate, "GATE_PATH", str(gate))
    monkeypatch.setattr(run_entry, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(
        run_entry, "TigerClient",
        lambda *a, **kw: SimpleNamespace(
            account_summary=lambda: SimpleNamespace(output={"net_liquidation": 1e6, "cash": 7e5}),
            positions=lambda: SimpleNamespace(
                output={"positions": [{"symbol": "ZZTEST", "quantity": 50}]}
            ),
        ),
    )

    assert cron_gate.is_gated()[0] is False               # no prior gate
    rc = run_entry.phase_init(tmp_path / "runs" / "r1")
    out = capsys.readouterr().out
    assert rc == 2
    assert "PHASE_INIT_GATED reason=presession_orphan_sweep" in out
    assert "ZZTEST" in out
    gated, doc = cron_gate.is_gated()
    assert gated is True and "ZZTEST" in doc["payload"]["orphans"]
