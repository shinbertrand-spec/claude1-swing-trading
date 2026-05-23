"""Tests for tools.pe_expansion_check."""
from __future__ import annotations

import math

import pytest

from tools.pe_expansion_check import compute


def test_doubled_pe_triggers_warning():
    e = compute(baseline_pe=18.0, current_pe=38.0)
    assert e.output["pe_expanded"] is True
    assert e.output["warning_late_stage"] is True
    assert math.isclose(e.output["expansion_ratio"], 38.0 / 18.0, rel_tol=1e-9)


def test_no_warning_when_not_doubled():
    e = compute(baseline_pe=18.0, current_pe=25.0)
    assert e.output["pe_expanded"] is False
    assert e.output["warning_late_stage"] is False


def test_custom_threshold():
    e = compute(baseline_pe=10.0, current_pe=15.0, threshold_ratio=1.5)
    assert e.output["pe_expanded"] is True


def test_negative_pe_rejected():
    with pytest.raises(ValueError, match="baseline_pe"):
        compute(baseline_pe=-5.0, current_pe=10.0)
    with pytest.raises(ValueError, match="current_pe"):
        compute(baseline_pe=10.0, current_pe=0.0)


def test_v1_flag_set():
    e = compute(baseline_pe=10.0, current_pe=20.0)
    assert e.output["v1_preliminary_flag"] is True
