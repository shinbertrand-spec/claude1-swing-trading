"""Phase 7 / H1 — Gate 6 strong-bear path: bull_strength=3, bear_strength=8
→ REJECT. Plus the already-fired override: any bull strength + any
already-fired risk-trigger → REJECT."""
from __future__ import annotations

import pytest

from tools.contract import SwingVerdict
from tools.debate_synthesis import verdict_from_strengths


def test_strong_bear_3_8_emits_reject():
    verdict, _ = verdict_from_strengths(3, 8)
    assert verdict is SwingVerdict.REJECT


@pytest.mark.parametrize(
    "bull,bear",
    [
        (0, 8),
        (0, 10),
        (3, 8),
        (3, 10),
    ],
)
def test_strict_reject_zone(bull: int, bear: int):
    verdict, _ = verdict_from_strengths(bull, bear)
    assert verdict is SwingVerdict.REJECT


@pytest.mark.parametrize("bull", [0, 1, 3, 5, 7, 9, 10])
def test_any_bull_strength_with_already_fired_trigger_rejects(bull: int):
    verdict, failure_mode = verdict_from_strengths(bull, 0, already_fired=True)
    assert verdict is SwingVerdict.REJECT
    assert failure_mode is None


def test_already_fired_override_beats_floor_path():
    # Even an A+ bull + INVALIDATION_WEAK bear is REJECTed if a risk
    # trigger has fired (price already below the proposed stop, etc.).
    from tools.contract import BearVerdict

    verdict, _ = verdict_from_strengths(
        9,
        2,
        already_fired=True,
        bull_grade="A+",
        bear_verdict=BearVerdict.INVALIDATION_WEAK,
        all_gates_passed=True,
    )
    assert verdict is SwingVerdict.REJECT
