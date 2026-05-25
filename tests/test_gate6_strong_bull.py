"""Phase 7 / H1 — Gate 6 strong-bull path: bull_strength=9, bear_strength=2
→ ENTRY_STRONG."""
from __future__ import annotations

import pytest

from tools.contract import BearVerdict, SwingVerdict
from tools.debate_synthesis import verdict_from_strengths


def test_strong_bull_9_2_emits_entry_strong():
    verdict, failure_mode = verdict_from_strengths(9, 2)
    assert verdict is SwingVerdict.ENTRY_STRONG
    assert failure_mode is None


@pytest.mark.parametrize(
    "bull,bear",
    [
        (8, 0),
        (8, 3),
        (9, 0),
        (9, 3),
        (10, 0),
        (10, 3),
    ],
)
def test_strict_entry_strong_zone(bull: int, bear: int):
    verdict, _ = verdict_from_strengths(bull, bear)
    assert verdict is SwingVerdict.ENTRY_STRONG


def test_invalidation_weak_plus_a_plus_grade_floor_path():
    # Even with bull_strength=6 and bear_strength=2 (would normally be
    # ENTRY_NORMAL by the table), the floor override fires when the bear
    # gives INVALIDATION_WEAK and the bull is A+/A grade.
    verdict, _ = verdict_from_strengths(
        6,
        2,
        bull_grade="A+",
        bear_verdict=BearVerdict.INVALIDATION_WEAK,
        all_gates_passed=True,
    )
    assert verdict is SwingVerdict.ENTRY_STRONG


def test_floor_override_requires_all_gates_passed():
    verdict, _ = verdict_from_strengths(
        6,
        2,
        bull_grade="A+",
        bear_verdict=BearVerdict.INVALIDATION_WEAK,
        all_gates_passed=False,  # one of Gates 1-5 failed
    )
    # Floor override does NOT fire; standard table applies → ENTRY_NORMAL.
    assert verdict is SwingVerdict.ENTRY_NORMAL
