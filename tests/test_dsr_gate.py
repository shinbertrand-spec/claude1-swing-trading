"""Tests for the C1 Deflated-Sharpe 7th gate (tools.backtest.dsr_gate).

The underlying DSR math is pinned to the published worked example in
tests/test_sharpe_stats.py; these tests cover the gate plumbing.
"""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest
import yaml

from tools.backtest import dsr_gate


def _combo(sharpe_ann: float, r_multiples=None, n_trades=50):
    rep = SimpleNamespace(
        returns=SimpleNamespace(sharpe_annualised=sharpe_ann),
        trades=SimpleNamespace(n_trades=n_trades),
    )
    outcomes = [SimpleNamespace(r_multiple=r) for r in (r_multiples or [])]
    return {"oos_report": rep, "concat_oos_outcomes": outcomes}


def _r_series(n=200, seed=3):
    import random
    rng = random.Random(seed)
    return [rng.gauss(0.1, 1.0) for _ in range(n)]


class TestEvaluateGrid:
    def test_single_combo_returns_none(self):
        assert dsr_gate.evaluate_grid([_combo(1.5)], registry_trials=10) is None

    def test_report_only_by_default(self):
        block = dsr_gate.evaluate_grid(
            [_combo(1.5, _r_series()), _combo(0.8), _combo(0.2)],
            registry_trials=50,
        )
        assert block["enforced"] is False
        assert 0.0 <= block["dsr"] <= 1.0
        assert block["n_trials"] == 50

    def test_enforced_mode_sets_flag(self):
        block = dsr_gate.evaluate_grid(
            [_combo(1.5, _r_series()), _combo(0.8)],
            registry_trials=50, dsr_min=0.95,
        )
        assert block["enforced"] is True
        assert block["passed"] in (True, False)

    def test_more_trials_lower_dsr(self):
        combos = [_combo(1.5, _r_series()), _combo(0.8), _combo(0.2)]
        few = dsr_gate.evaluate_grid(combos, registry_trials=5)
        many = dsr_gate.evaluate_grid(combos, registry_trials=500)
        assert many["dsr"] < few["dsr"]

    def test_moments_come_from_outcomes(self):
        block = dsr_gate.evaluate_grid(
            [_combo(1.5, _r_series()), _combo(0.8)], registry_trials=20,
        )
        # a real series was provided -> no Normal-assumption caveat
        assert "Normal moments assumed" not in block["note"]
        assert block["t_obs"] == 200

    def test_missing_outcomes_falls_back_to_normal_with_caveat(self):
        block = dsr_gate.evaluate_grid(
            [_combo(1.5), _combo(0.8)], registry_trials=20,
        )
        assert "Normal moments assumed" in block["note"]
        assert block["skew"] == 0.0
        assert block["kurt_raw"] == 3.0

    def test_zero_variance_grid_is_undefined_not_crash(self):
        block = dsr_gate.evaluate_grid(
            [_combo(1.0, _r_series()), _combo(1.0)], registry_trials=20,
        )
        assert block["dsr"] is None
        assert "zero variance" in block["note"]

    def test_registry_floor_applies(self):
        # registry smaller than the grid -> grid size wins
        block = dsr_gate.evaluate_grid(
            [_combo(1.5, _r_series()), _combo(0.8), _combo(0.3)],
            registry_trials=2,
        )
        assert block["n_trials"] == 3

    def test_format_block_renders(self):
        block = dsr_gate.evaluate_grid(
            [_combo(1.5, _r_series()), _combo(0.8)], registry_trials=20,
        )
        text = "\n".join(dsr_gate.format_block(block))
        assert "Deflated Sharpe" in text
        assert "DSR" in text
        assert "\n".join(dsr_gate.format_block(None))  # degenerate renders too


class TestTrialRegistry:
    def test_derivation_counts_grids_and_sweeps(self, tmp_path):
        spec_dir = tmp_path / "specs"
        spec_dir.mkdir()
        (spec_dir / "a.yml").write_text(yaml.safe_dump({
            "kind": "x", "params": {"lookback": [10, 20, 30], "top_k": [4, 8]},
        }), encoding="utf-8")
        (spec_dir / "not_a_spec.yml").write_text("just: metadata\n", encoding="utf-8")
        sweep_dir = tmp_path / "sweeps"
        sweep_dir.mkdir()
        (sweep_dir / "s.json").write_text(json.dumps([
            {"lookback": 10, "oos_agg": {"sharpe": 1.0}},
            {"lookback": 20, "oos_agg": {"sharpe": 0.5}},
        ]), encoding="utf-8")
        comps = dsr_gate.derive_trial_components(spec_dir, sweep_dir)
        by_source = {c["source"]: c["n_trials"] for c in comps}
        assert by_source["spec-grid:a.yml"] == 6      # 3 x 2
        assert by_source["sweep:s.json"] == 2
        assert "spec-grid:not_a_spec.yml" not in by_source

    def test_registry_roundtrip(self, tmp_path):
        comps = [{"source": "test", "n_trials": 7, "detail": "d"}]
        p = dsr_gate.write_registry(comps, tmp_path / "trials.yml")
        assert dsr_gate.registry_total(p) == 7

    def test_missing_registry_derives_live(self, tmp_path):
        # nonexistent path -> falls back to live derivation (real dirs), > 0
        assert dsr_gate.registry_total(tmp_path / "nope.yml") > 0
