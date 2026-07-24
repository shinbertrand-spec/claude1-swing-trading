"""Tests for the B1 gate-chain wiring in pipeline.place_candidate
(2026-07-24, Phase 1). The chain itself is covered by
test_auto_paper_gate_chain.py; these tests cover the SEAM: veto propagation,
size reduction, fail-closed error boundary, and the _run_gate_chain helper's
real path against a synthetic snapshot."""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tools.auto_paper import gate_chain
from tools.auto_paper import pipeline as pl
from tools.auto_paper import screener as _screener_mod
from tools.auto_paper import state
from tools.auto_paper.pipeline import CandidateInput, place_candidate


def _cand(**over):
    base = dict(
        ticker="NVDA", setup_type="EP", setup_grade="A",
        pivot_price=850.00, limit_price=850.50, stop_price=820.00,
        target_price=910.00, shares=10, sector_etf="XLK",
    )
    base.update(over)
    return CandidateInput(**base)


class _FakeTrade:
    """Minimal TigerClient _trade_client seam (mirrors the sibling test file)."""
    def __init__(self):
        self.account_full = "PAPER87654321"
        self.config_info = {
            "tiger_id_masked": "...5678", "account_masked": "...4321",
            "license": "TBSG", "is_paper": True,
            "server_url": "https://mock", "props_dir": "/mock",
        }
        self.calls: list[tuple] = []

    def get_assets(self, *, account, segment=False, **_):
        return [SimpleNamespace(summary=SimpleNamespace(
            cash=950_000.0, available_funds=950_000.0, buying_power=1_900_000.0,
            net_liquidation=1_000_000.0, gross_position_value=50_000.0,
            currency="USD"))]

    def get_positions(self, *, account, **_):
        return []

    def get_open_orders(self, *, account, **_):
        return []

    def get_contract(self, *, symbol, **_):
        return SimpleNamespace(symbol=symbol, sec_type="STK", currency="USD")

    def place_order(self, order):
        self.calls.append(("place_order", order.contract.symbol))
        order.id = 99999
        return 99999


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "PAPER_AUTO_LEDGER_DIR", str(tmp_path / "ledgers"))
    monkeypatch.setattr(
        state, "PAPER_AUTO_POSITIONS_JSON",
        str(tmp_path / "journal" / "positions.json"),
    )
    monkeypatch.setattr(pl.config, "is_deployable", lambda t: t == "EP")
    monkeypatch.setattr(pl, "_resolve_regime_multiplier",
                        lambda: ("stage_2_confirmed", 1.0))
    monkeypatch.setattr(pl, "_load_discretionary_open_positions", lambda: [])
    monkeypatch.setenv("CLUSTER_CALIB_DIR", str(tmp_path / "cluster-calib"))
    monkeypatch.setenv(gate_chain.GATE_LOG_DIR_ENV, str(tmp_path / "gates"))
    monkeypatch.setenv(gate_chain.BREAKER_STATE_ENV, str(tmp_path / "breaker.json"))

    def _clean_screener(ticker, claimed_sector_etf):
        return _screener_mod.ScreenerResult(
            ticker=ticker, blocked=False, blocking_checks=[],
            corrected_sector_etf=None,
            checks=[_screener_mod.CheckResult(check="litigation", passed=True)],
            computed_at="2026-07-24T00:00:00+00:00",
        )
    monkeypatch.setattr(pl, "_run_screener", _clean_screener)

    from tools.broker.tiger import TigerClient
    fake = _FakeTrade()
    return TigerClient(_trade_client=fake), fake, tmp_path


class TestWiringSeam:
    def test_chain_veto_rejects_before_broker(self, harness, monkeypatch):
        client, fake, _ = harness
        monkeypatch.setattr(
            pl, "_run_gate_chain",
            lambda cand, **kw: ("gate_chain:liquidity — ADV unknown", None),
        )
        res = place_candidate(_cand(), client=client, dry_run=False)
        assert res.status == "rejected"
        assert res.reason.startswith("gate_chain:liquidity")
        assert fake.calls == []  # broker never called

    def test_chain_reduction_applies_to_size(self, harness, monkeypatch):
        client, _, _ = harness
        monkeypatch.setattr(pl, "_run_gate_chain", lambda cand, **kw: (None, 4))
        res = place_candidate(_cand(shares=10), client=client, dry_run=True)
        assert res.status == "dry_run"
        assert res.cost_estimate_usd == pytest.approx(4 * 850.50)

    def test_chain_error_fails_closed(self, harness, monkeypatch):
        client, fake, _ = harness
        def _boom(cand, **kw):
            raise RuntimeError("snapshot exploded")
        monkeypatch.setattr(pl, "_run_gate_chain", _boom)
        res = place_candidate(_cand(), client=client, dry_run=False)
        assert res.status == "rejected"
        assert "fail-closed" in res.reason
        assert fake.calls == []


class TestRunGateChainHelper:
    def _snapshot(self, cand):
        rng = np.random.default_rng(1)
        idx = pd.bdate_range("2026-04-01", periods=80)
        return gate_chain.BookSnapshot(
            account_net_liq=1_000_000.0, cash=950_000.0,
            open_positions=[], discretionary_positions=[],
            dollar_adv_20d=500_000_000.0,
            candidate_returns=pd.Series(rng.normal(0.001, 0.02, 80), index=idx),
            book_returns={}, open_themes={},
            sleeve_equity=1_000_000.0,
        )

    @pytest.fixture
    def helper_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv(gate_chain.GATE_LOG_DIR_ENV, str(tmp_path / "gates"))
        monkeypatch.setenv(gate_chain.BREAKER_STATE_ENV, str(tmp_path / "breaker.json"))
        monkeypatch.setattr(pl, "_setup_priors", lambda t: (0.55, 2.0))
        monkeypatch.setattr(
            gate_chain, "build_snapshot", lambda c, **kw: self._snapshot(c),
        )
        return tmp_path

    def test_pass_path_logs_and_returns_none(self, helper_env):
        reason, shares = pl._run_gate_chain(
            _cand(), net_liq=1_000_000.0, cash=950_000.0,
            open_positions=[], discretionary_positions=[],
            regime_class=None, dry_run=True,
        )
        assert reason is None
        log_files = list((helper_env / "gates").glob("*.jsonl"))
        assert log_files, "gate log must be written"
        rows = [json.loads(x) for x in log_files[0].read_text().splitlines()]
        assert any(r.get("kind") == "chain_summary" for r in rows)
        assert all(r.get("source") == "paper-auto-pipeline" for r in rows)
        # dry run must NOT persist breaker state
        assert not (helper_env / "breaker.json").exists()

    def test_real_run_persists_breaker_state(self, helper_env):
        pl._run_gate_chain(
            _cand(), net_liq=1_000_000.0, cash=950_000.0,
            open_positions=[], discretionary_positions=[],
            regime_class=None, dry_run=False,
        )
        assert (helper_env / "breaker.json").exists()

    def test_tripped_breaker_vetoes(self, helper_env):
        gate_chain.save_breaker_state(
            {"tripped": True, "tripped_at": "x", "tripped_dd_pct": 0.25,
             "high_water": 1.0},
        )
        reason, _ = pl._run_gate_chain(
            _cand(), net_liq=1_000_000.0, cash=950_000.0,
            open_positions=[], discretionary_positions=[],
            regime_class=None, dry_run=True,
        )
        assert reason is not None
        assert reason.startswith("gate_chain:circuit_breaker")
