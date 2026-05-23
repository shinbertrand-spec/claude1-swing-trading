"""Tests for tools.regime_check — pure classification logic.

The composition function fetches live data, so we test the pure classifier
``classify_broad`` and rely on the fact that ``compute`` is a straight
composition (tested manually via CLI).
"""
from __future__ import annotations

import math

from tools.regime_check import classify_broad


def test_classify_7_of_7():
    cls, mult = classify_broad(7)
    assert cls == "stage_2_confirmed"
    assert math.isclose(mult, 1.0)


def test_classify_5_or_6():
    for p in (5, 6):
        cls, mult = classify_broad(p)
        assert cls == "stage_2_weakening"
        assert math.isclose(mult, 0.75)


def test_classify_3_or_4():
    for p in (3, 4):
        cls, mult = classify_broad(p)
        assert cls == "stage_3_transitional"
        assert math.isclose(mult, 0.5)


def test_classify_stage_4():
    for p in (0, 1, 2):
        cls, mult = classify_broad(p)
        assert cls == "stage_4"
        assert math.isclose(mult, 0.0)
