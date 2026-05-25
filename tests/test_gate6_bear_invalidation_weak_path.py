"""Phase 7 / H1 — Gate 6 INVALIDATION_WEAK + A-grade floor path.

Per H1 spec §6 edge case: "Bear verdict: INVALIDATION_WEAK AND bull A+/A
grade AND all Gates 1-5 pass → ENTRY_STRONG candidate (still scored, but the
floor is high)."

The test confirms the override fires even when the underlying strengths
would otherwise put the decision in a lower bucket."""
from __future__ import annotations

import pytest

from tools.contract import BearVerdict, SwingVerdict
from tools.debate_synthesis import verdict_from_strengths


@pytest.mark.parametrize("grade", ["A+", "A"])
def test_invalidation_weak_plus_a_grade_emits_entry_strong(grade: str):
    # bull_strength=7 alone would emit ENTRY_NORMAL; floor lifts to ENTRY_STRONG.
    verdict, _ = verdict_from_strengths(
        7,
        2,
        bull_grade=grade,
        bear_verdict=BearVerdict.INVALIDATION_WEAK,
        all_gates_passed=True,
    )
    assert verdict is SwingVerdict.ENTRY_STRONG


@pytest.mark.parametrize("grade", ["B+", "B", "C", None])
def test_invalidation_weak_plus_non_a_grade_does_not_lift(grade):
    verdict, _ = verdict_from_strengths(
        7,
        2,
        bull_grade=grade,
        bear_verdict=BearVerdict.INVALIDATION_WEAK,
        all_gates_passed=True,
    )
    # Without A-grade, fall through to plain decision-table cell (7, 2)
    # → ENTRY_NORMAL.
    assert verdict is SwingVerdict.ENTRY_NORMAL


@pytest.mark.parametrize(
    "bear_verdict",
    [BearVerdict.INVALIDATION_PARTIAL, BearVerdict.INVALIDATION_STRONG],
)
def test_non_invalidation_weak_does_not_lift(bear_verdict: BearVerdict):
    verdict, _ = verdict_from_strengths(
        7,
        2,
        bull_grade="A+",
        bear_verdict=bear_verdict,
        all_gates_passed=True,
    )
    assert verdict is SwingVerdict.ENTRY_NORMAL


def test_already_fired_beats_floor_override():
    # If a risk trigger fires, REJECT regardless of grade / bear verdict.
    verdict, _ = verdict_from_strengths(
        7,
        2,
        already_fired=True,
        bull_grade="A+",
        bear_verdict=BearVerdict.INVALIDATION_WEAK,
        all_gates_passed=True,
    )
    assert verdict is SwingVerdict.REJECT
