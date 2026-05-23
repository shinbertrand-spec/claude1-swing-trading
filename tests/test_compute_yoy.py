"""Tests for tools.compute_yoy."""
from __future__ import annotations

import math

import pytest

from tools.compute_yoy import compute


def test_positive_growth():
    e = compute(1.87, 1.55)
    assert math.isclose(e.output["yoy_growth_decimal"], 1.87 / 1.55 - 1.0, rel_tol=1e-9)
    assert math.isclose(e.output["yoy_growth_pct"], (1.87 / 1.55 - 1.0) * 100.0, rel_tol=1e-9)


def test_negative_growth():
    e = compute(80.0, 100.0)
    assert math.isclose(e.output["yoy_growth_decimal"], -0.20, rel_tol=1e-9)


def test_zero_prior_rejected():
    with pytest.raises(ValueError, match="positive"):
        compute(1.0, 0.0)


def test_negative_prior_rejected():
    with pytest.raises(ValueError, match="positive"):
        compute(1.0, -5.0)


def test_trace_entry_shape():
    e = compute(2.0, 1.0)
    assert e.tool == "tools/compute_yoy.py"
    assert e.inputs == {"current": 2.0, "prior": 1.0}
    assert e.id is None
    assert e.fetched_at  # set to default
    d = e.to_dict()
    assert "id" not in d  # stripped when None
