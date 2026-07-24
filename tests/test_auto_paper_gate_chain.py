"""Tests for the B1 pre-order gate chain (tools.auto_paper.gate_chain).

All gates are pure — tests build synthetic BookSnapshots, no network/disk
except the tmp-path log/state writes.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from tools.auto_paper import gate_chain as gc


def _cand(**kw) -> gc.GateCandidate:
    base = dict(
        ticker="TEST", direction="long", stated_probability=0.55,
        proposed_shares=100, price=50.0, stop_price=46.0, target_price=62.0,
        sector_etf="XLK", theme=None,
    )
    base.update(kw)
    return gc.GateCandidate(**base)


def _snap(**kw) -> gc.BookSnapshot:
    base = dict(
        account_net_liq=100_000.0, cash=80_000.0,
        open_positions=[], discretionary_positions=[],
        dollar_adv_20d=50_000_000.0,
    )
    base.update(kw)
    return gc.BookSnapshot(**base)


def _returns(seed: int = 1, n: int = 80) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2026-04-01", periods=n)
    return pd.Series(rng.normal(0.0005, 0.02, size=n), index=idx)


# ---------------------------------------------------------------------------
# Gate 1 — sizing bounds
# ---------------------------------------------------------------------------

class TestSizingBounds:
    def test_no_edge_fails(self):
        # p=0.30 with no target -> assumed b=2: kelly = 0.30 - 0.70/2 = -0.05
        r = gc.gate_sizing_bounds(
            _cand(stated_probability=0.30, target_price=None), _snap(), gc.GateConfig()
        )
        assert not r.passed
        assert "no edge" in r.reason

    def test_short_direction_refused(self):
        r = gc.gate_sizing_bounds(_cand(direction="short"), _snap(), gc.GateConfig())
        assert not r.passed
        assert "long-only" in r.reason

    def test_probability_bounds(self):
        for p in (0.0, 1.0, -0.2, 1.5):
            r = gc.gate_sizing_bounds(_cand(stated_probability=p), _snap(), gc.GateConfig())
            assert not r.passed

    def test_reduces_oversized_proposal(self):
        # p=0.55, b=(62-50)/(50-46)=3 -> kelly = 0.55-0.45/3 = 0.40
        # applied = min(0.5*0.40, 0.05) = 0.05 -> 100k*0.05/50 = 100 shares max
        r = gc.gate_sizing_bounds(_cand(proposed_shares=500), _snap(), gc.GateConfig())
        assert r.passed
        assert r.adjusted_shares == 100
        assert r.detail["kelly_f"] == pytest.approx(0.40, abs=1e-9)

    def test_within_bound_keeps_proposed_size(self):
        r = gc.gate_sizing_bounds(_cand(proposed_shares=80), _snap(), gc.GateConfig())
        assert r.passed
        assert r.adjusted_shares is None  # never sizes up

    def test_band_clamp_binds_before_kelly(self):
        cfg = gc.GateConfig(band_max_pct=0.01)  # 1% band -> 20 shares at $50
        r = gc.gate_sizing_bounds(_cand(proposed_shares=100), _snap(), cfg)
        assert r.passed
        assert r.adjusted_shares == 20

    def test_calibration_fields_logged(self):
        r = gc.gate_sizing_bounds(_cand(), _snap(), gc.GateConfig())
        for key in ("stated_probability", "payoff_ratio_b", "kelly_f", "applied_fraction"):
            assert key in r.detail

    def test_missing_target_uses_assumed_payoff(self):
        r = gc.gate_sizing_bounds(
            _cand(target_price=None), _snap(), gc.GateConfig(assumed_payoff_ratio=2.0)
        )
        assert r.detail["payoff_ratio_b"] == 2.0


# ---------------------------------------------------------------------------
# Gate 2 — liquidity
# ---------------------------------------------------------------------------

class TestLiquidity:
    def test_unknown_adv_fails_conservative(self):
        r = gc.gate_liquidity(_cand(), _snap(dollar_adv_20d=None), gc.GateConfig(), 100)
        assert not r.passed
        assert "unknown" in r.reason

    def test_within_cap_passes(self):
        # cost 100*50=5k vs 5% of 50M = 2.5M
        r = gc.gate_liquidity(_cand(), _snap(), gc.GateConfig(), 100)
        assert r.passed
        assert r.adjusted_shares is None

    def test_over_cap_reduces(self):
        # ADV 80k -> cap 4k -> 80 shares at $50
        r = gc.gate_liquidity(_cand(), _snap(dollar_adv_20d=80_000.0), gc.GateConfig(), 100)
        assert r.passed
        assert r.adjusted_shares == 80

    def test_cap_below_one_share_fails(self):
        r = gc.gate_liquidity(_cand(), _snap(dollar_adv_20d=500.0), gc.GateConfig(), 100)
        assert not r.passed

    def test_boundary_exact_cap_passes_unreduced(self):
        # cap = 5% * 100k = 5000 = exactly 100 shares * $50
        r = gc.gate_liquidity(_cand(), _snap(dollar_adv_20d=100_000.0), gc.GateConfig(), 100)
        assert r.passed
        assert r.adjusted_shares is None


# ---------------------------------------------------------------------------
# Gate 3 — correlation / theme overlap
# ---------------------------------------------------------------------------

class TestCorrelation:
    def test_empty_book_passes(self):
        r = gc.gate_correlation(_cand(), _snap(), gc.GateConfig())
        assert r.passed

    def test_duplicate_theme_rejected(self):
        snap = _snap(
            open_positions=[{"ticker": "NVDA", "shares": 10, "entry_price": 100.0}],
            open_themes={"NVDA": "ai_momentum"},
        )
        r = gc.gate_correlation(_cand(theme="ai_momentum"), snap, gc.GateConfig())
        assert not r.passed
        assert "ai_momentum" in r.reason

    def test_high_correlation_rejected(self):
        base = _returns(seed=3)
        snap = _snap(
            open_positions=[{"ticker": "HELD", "shares": 10, "entry_price": 100.0}],
            candidate_returns=base,
            book_returns={"HELD": base * 1.0},  # corr = 1.0
        )
        r = gc.gate_correlation(_cand(), snap, gc.GateConfig())
        assert not r.passed
        assert "corr(" in r.reason

    def test_low_correlation_passes(self):
        snap = _snap(
            open_positions=[{"ticker": "HELD", "shares": 10, "entry_price": 100.0}],
            candidate_returns=_returns(seed=3),
            book_returns={"HELD": _returns(seed=99)},  # independent draws
        )
        r = gc.gate_correlation(_cand(), snap, gc.GateConfig())
        assert r.passed

    def test_missing_candidate_history_fails_conservative(self):
        snap = _snap(
            open_positions=[{"ticker": "HELD", "shares": 10, "entry_price": 100.0}],
            candidate_returns=None,
        )
        r = gc.gate_correlation(_cand(), snap, gc.GateConfig())
        assert not r.passed

    def test_missing_book_history_skips_pair_but_passes(self):
        snap = _snap(
            open_positions=[{"ticker": "HELD", "shares": 10, "entry_price": 100.0}],
            candidate_returns=_returns(seed=3),
            book_returns={},
        )
        r = gc.gate_correlation(_cand(), snap, gc.GateConfig())
        assert r.passed
        assert r.detail["skipped_no_data"] == ["HELD"]


# ---------------------------------------------------------------------------
# Gate 4 — concentration
# ---------------------------------------------------------------------------

class TestConcentration:
    def test_position_count_cap(self):
        positions = [
            {"ticker": f"T{i}", "shares": 1, "entry_price": 1.0, "stage": "starter"}
            for i in range(8)
        ]
        r = gc.gate_concentration(_cand(), _snap(open_positions=positions), gc.GateConfig(), 10)
        assert not r.passed
        assert "count limit" in r.reason

    def test_per_position_cap(self):
        # 200 shares * $50 = 10k = 10% of 100k > 5%
        r = gc.gate_concentration(_cand(), _snap(), gc.GateConfig(), 200)
        assert not r.passed
        assert "cap" in r.reason

    def test_sector_cap(self):
        positions = [
            {"ticker": "A", "shares": 100, "entry_price": 180.0, "sector": "XLK",
             "stage": "starter"},
        ]
        # existing 18k + new 4k = 22k = 22% of 100k > 20%
        r = gc.gate_concentration(
            _cand(sector_etf="XLK"), _snap(open_positions=positions), gc.GateConfig(), 80,
        )
        assert not r.passed
        assert "XLK" in r.reason

    def test_clean_book_passes(self):
        r = gc.gate_concentration(_cand(), _snap(), gc.GateConfig(), 80)
        assert r.passed


# ---------------------------------------------------------------------------
# Gate 5 — circuit breaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def test_first_run_sets_hwm_and_passes(self):
        r, state = gc.gate_circuit_breaker(
            _snap(sleeve_equity=100_000.0), gc.GateConfig(), {},
        )
        assert r.passed
        assert state["high_water"] == 100_000.0

    def test_trip_at_threshold(self):
        state = {"high_water": 100_000.0}
        r, new_state = gc.gate_circuit_breaker(
            _snap(sleeve_equity=80_000.0), gc.GateConfig(), state,
        )
        assert not r.passed
        assert new_state["tripped"] is True
        assert "TRIPPED" in r.reason

    def test_below_threshold_no_trip(self):
        state = {"high_water": 100_000.0}
        r, new_state = gc.gate_circuit_breaker(
            _snap(sleeve_equity=81_000.0), gc.GateConfig(), state,
        )
        assert r.passed
        assert not new_state.get("tripped")

    def test_tripped_state_fails_until_reset(self):
        state = {"tripped": True, "tripped_at": "2026-07-24T00:00:00+00:00",
                 "tripped_dd_pct": 0.25, "high_water": 100_000.0}
        # even at full recovery, a tripped breaker stays tripped
        r, new_state = gc.gate_circuit_breaker(
            _snap(sleeve_equity=120_000.0), gc.GateConfig(), state,
        )
        assert not r.passed
        assert new_state["tripped"] is True

    def test_manual_reset_path(self, tmp_path):
        state_path = tmp_path / "breaker.json"
        log_dir = tmp_path / "gates"
        gc.save_breaker_state(
            {"tripped": True, "tripped_at": "x", "tripped_equity": 80_000.0,
             "high_water": 100_000.0},
            state_path,
        )
        new_state = gc.reset_breaker(
            operator="bertrand", note="post-incident reset", path=state_path,
            log_dir=log_dir,
        )
        assert new_state["tripped"] is False
        assert new_state["reset_by"] == "bertrand"
        # HWM re-based to tripped equity so recovery doesn't insta-re-trip
        assert new_state["high_water"] == 80_000.0
        lines = [
            json.loads(line)
            for f in log_dir.glob("*.jsonl")
            for line in f.read_text().splitlines()
        ]
        assert any(rec["kind"] == "breaker_reset" for rec in lines)

    def test_reset_refused_when_not_tripped(self, tmp_path):
        state_path = tmp_path / "breaker.json"
        gc.save_breaker_state({"tripped": False, "high_water": 1.0}, state_path)
        with pytest.raises(RuntimeError, match="not tripped"):
            gc.reset_breaker(operator="x", note="y", path=state_path,
                             log_dir=tmp_path / "gates")


# ---------------------------------------------------------------------------
# Chain runner + log
# ---------------------------------------------------------------------------

class TestChain:
    def test_all_pass_produces_five_rows_and_final_size(self, tmp_path):
        chain, state = gc.run_chain(_cand(proposed_shares=80), _snap())
        assert chain.passed
        assert chain.final_shares == 80
        assert [r.gate for r in chain.results] == list(gc.GATE_NAMES)
        path = gc.append_gate_log(chain, source="test", log_dir=tmp_path)
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(lines) == 6  # 5 gate rows + 1 chain summary
        assert lines[-1]["kind"] == "chain_summary"
        assert {r["gate"] for r in lines[:5]} == set(gc.GATE_NAMES)

    def test_any_fail_vetoes_but_still_logs_all_gates(self):
        chain, _ = gc.run_chain(
            _cand(stated_probability=0.30, target_price=None), _snap(),  # gate 1 fails
        )
        assert not chain.passed
        assert chain.final_shares == 0
        assert len(chain.results) == 5  # no short-circuit

    def test_size_adjustment_flows_downstream(self):
        # Kelly reduces 500 -> 100; concentration then sees 100*50=5k = 5% (pass)
        chain, _ = gc.run_chain(_cand(proposed_shares=500), _snap())
        assert chain.passed
        assert chain.final_shares == 100

    def test_breaker_state_threaded_through(self):
        chain, state = gc.run_chain(
            _cand(), _snap(sleeve_equity=70_000.0),
            breaker_state={"high_water": 100_000.0},
        )
        assert not chain.passed
        assert state["tripped"] is True
