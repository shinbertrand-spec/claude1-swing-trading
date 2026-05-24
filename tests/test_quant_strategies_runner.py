"""Tests for the quant-strategies runner.

Focused on the runner's contract — grid expansion, spec parsing, gate
verdict computation, report shape. End-to-end real-data backtests are
exercised via the slash command / subagent, not here.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.quant_strategies.runner import _expand_grid, _identify_varying_params


def test_expand_grid_no_lists_returns_single_dict():
    out = _expand_grid({"a": 1, "b": 2})
    assert out == [{"a": 1, "b": 2}]


def test_expand_grid_single_list_axis():
    out = _expand_grid({"a": [1, 2, 3], "b": 10})
    assert len(out) == 3
    assert {"a": 1, "b": 10} in out
    assert {"a": 2, "b": 10} in out
    assert {"a": 3, "b": 10} in out


def test_expand_grid_cartesian_product():
    out = _expand_grid({"a": [1, 2], "b": [10, 20]})
    assert len(out) == 4
    expected = [
        {"a": 1, "b": 10}, {"a": 1, "b": 20},
        {"a": 2, "b": 10}, {"a": 2, "b": 20},
    ]
    for e in expected:
        assert e in out


def test_expand_grid_empty_input():
    out = _expand_grid({})
    assert out == [{}]


def test_identify_varying_params_all_same():
    dicts = [{"a": 1, "b": 2}, {"a": 1, "b": 2}]
    assert _identify_varying_params(dicts) == []


def test_identify_varying_params_one_varies():
    dicts = [{"a": 1, "b": 2}, {"a": 1, "b": 3}]
    assert _identify_varying_params(dicts) == ["b"]


def test_identify_varying_params_multiple_vary():
    dicts = [{"a": 1, "b": 2, "c": 5}, {"a": 2, "b": 2, "c": 6}]
    assert set(_identify_varying_params(dicts)) == {"a", "c"}


def test_identify_varying_params_empty():
    assert _identify_varying_params([]) == []


def test_clenow_spec_yaml_is_loadable():
    """The shipped reference spec must round-trip through yaml.safe_load."""
    spec_path = Path("tools/quant_strategies/clenow_momentum.yml")
    assert spec_path.exists(), f"spec missing: {spec_path}"
    spec = yaml.safe_load(spec_path.read_text())
    # Required top-level sections.
    for key in ("meta", "kind", "universe", "period", "params", "gate"):
        assert key in spec, f"spec missing top-level key: {key}"
    assert spec["kind"] == "clenow_momentum"
    assert "tickers" in spec["universe"]
    assert "benchmark" in spec["universe"]
    assert spec["universe"]["benchmark"] in spec["universe"]["tickers"] + ["SPY"]
    assert "start" in spec["period"] and "end" in spec["period"]


def test_clenow_spec_kind_is_in_registry():
    """The spec's kind must resolve to a registered plugin."""
    from tools.quant_strategies._kinds import KIND_REGISTRY
    spec_path = Path("tools/quant_strategies/clenow_momentum.yml")
    spec = yaml.safe_load(spec_path.read_text())
    assert spec["kind"] in KIND_REGISTRY
