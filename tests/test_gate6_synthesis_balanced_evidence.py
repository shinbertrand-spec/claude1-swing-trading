"""Phase 7 / H1 — Gate 6 balanced-evidence path.

When bull_strength == bear_strength == 5 (or any |gap| <= 2 within the 4-7
band) the facilitator emits WATCH_BUILD_THESIS with failure_mode set to
``balanced_evidence_no_clear_stance``. This is the spec's "reserve Hold for
genuinely balanced" discipline."""
from __future__ import annotations

import pytest

from tools.contract import SwingVerdict
from tools.debate_synthesis import verdict_from_strengths


def test_balanced_5_5_emits_watch_with_failure_mode():
    verdict, failure_mode = verdict_from_strengths(5, 5)
    assert verdict is SwingVerdict.WATCH_BUILD_THESIS
    assert failure_mode == "balanced_evidence_no_clear_stance"


@pytest.mark.parametrize(
    "bull,bear",
    [
        (4, 4),
        (5, 5),
        (6, 6),
        (7, 7),
        (4, 5),
        (5, 4),
        (6, 5),  # gap=1, both in 4-7 — balanced wins over ENTRY_NORMAL row
        (5, 6),  # gap=1, both in 4-7 — balanced wins over DEFER row
        (4, 6),  # gap=2
        (6, 4),  # gap=2
        (5, 7),  # gap=2
        (7, 5),  # gap=2
    ],
)
def test_balanced_band_emits_failure_mode(bull: int, bear: int):
    # NOTE: (6,5) and (5,6) — the table rows ENTRY_NORMAL (≥6, ≤5) and
    # DEFER (≤5, ≥6) match first in the priority order, so they get
    # ENTRY_NORMAL / DEFER, not WATCH. The balanced rule applies only when
    # the higher-priority rows don't fire.
    verdict, failure_mode = verdict_from_strengths(bull, bear)
    if bull >= 6 and bear <= 5:
        assert verdict is SwingVerdict.ENTRY_NORMAL
    elif bull <= 5 and bear >= 6:
        assert verdict is SwingVerdict.DEFER
    else:
        assert verdict is SwingVerdict.WATCH_BUILD_THESIS
        assert failure_mode == "balanced_evidence_no_clear_stance"


def test_balanced_gap_above_2_does_not_trigger_failure_mode():
    # gap=3, both in 4-7 → ENTRY_NORMAL (bull=7 ≥6 AND bear=4 ≤5)
    verdict, failure_mode = verdict_from_strengths(7, 4)
    assert verdict is SwingVerdict.ENTRY_NORMAL
    assert failure_mode is None


def test_outside_4_7_band_is_not_balanced_failure_mode():
    # (3, 3): neither in 4-7 → fallback WATCH without failure_mode
    verdict, failure_mode = verdict_from_strengths(3, 3)
    assert verdict is SwingVerdict.WATCH_BUILD_THESIS
    assert failure_mode is None
