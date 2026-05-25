"""Phase 7 / H1 — parametrized over all 25 cells of the bull × bear strength
grid (5 representative values per axis). Asserts the H1 spec §6 decision
table emits the documented verdict for every cell.

Representative values: {1, 3, 5, 7, 9} — one inside each table-row band
(≤3, 4-5, 4-7 middle, ≥6 lower-bound, ≥8)."""
from __future__ import annotations

import pytest

from tools.contract import SwingVerdict
from tools.debate_synthesis import verdict_from_strengths


# Expected (bull, bear) → verdict for the 25 cells. Derived from the §6
# priority-ordered decision table:
#
#   1. (bull ≤3, bear ≥8) → REJECT
#   2. (bull ≥8, bear ≤3) → ENTRY_STRONG
#   3. (bull ≥6, bear ≤5) → ENTRY_NORMAL
#   4. (bull ≤5, bear ≥6) → DEFER
#   5. (4-7, 4-7) and |gap| ≤ 2 → WATCH_BUILD_THESIS (balanced)
#   6. Fallback → WATCH_BUILD_THESIS (no failure mode)
#
GRID: dict[tuple[int, int], SwingVerdict] = {
    # bull=1 (≤3)
    (1, 1): SwingVerdict.WATCH_BUILD_THESIS,  # fallback — both very weak
    (1, 3): SwingVerdict.WATCH_BUILD_THESIS,  # fallback — bear=3 not ≥6
    (1, 5): SwingVerdict.WATCH_BUILD_THESIS,  # fallback — bear=5 not ≥6
    (1, 7): SwingVerdict.DEFER,  # bull≤5, bear≥6
    (1, 9): SwingVerdict.REJECT,  # bull≤3, bear≥8
    # bull=3 (≤3)
    (3, 1): SwingVerdict.WATCH_BUILD_THESIS,  # fallback
    (3, 3): SwingVerdict.WATCH_BUILD_THESIS,  # fallback
    (3, 5): SwingVerdict.WATCH_BUILD_THESIS,  # fallback
    (3, 7): SwingVerdict.DEFER,
    (3, 9): SwingVerdict.REJECT,
    # bull=5 (4-7 lower)
    (5, 1): SwingVerdict.WATCH_BUILD_THESIS,  # fallback — bull=5 not ≥6
    (5, 3): SwingVerdict.WATCH_BUILD_THESIS,  # fallback — bull=5 not ≥6
    (5, 5): SwingVerdict.WATCH_BUILD_THESIS,  # balanced (gap=0, both 4-7)
    (5, 7): SwingVerdict.DEFER,
    (5, 9): SwingVerdict.DEFER,
    # bull=7 (4-7 upper)
    (7, 1): SwingVerdict.ENTRY_NORMAL,
    (7, 3): SwingVerdict.ENTRY_NORMAL,
    (7, 5): SwingVerdict.ENTRY_NORMAL,
    (7, 7): SwingVerdict.WATCH_BUILD_THESIS,  # balanced (gap=0, both 4-7)
    (7, 9): SwingVerdict.WATCH_BUILD_THESIS,  # fallback — bear=9 not in 4-7
    # bull=9 (≥8)
    (9, 1): SwingVerdict.ENTRY_STRONG,
    (9, 3): SwingVerdict.ENTRY_STRONG,
    (9, 5): SwingVerdict.ENTRY_NORMAL,  # rule 3: bull≥6, bear≤5
    (9, 7): SwingVerdict.WATCH_BUILD_THESIS,  # fallback
    (9, 9): SwingVerdict.WATCH_BUILD_THESIS,  # fallback
}


@pytest.mark.parametrize("bull,bear,expected", [(b, c, v) for (b, c), v in GRID.items()])
def test_decision_table_25_cells(bull: int, bear: int, expected: SwingVerdict):
    verdict, failure_mode = verdict_from_strengths(bull, bear)
    assert verdict is expected, (
        f"bull={bull}, bear={bear}: expected {expected.value}, got {verdict.value}"
    )
    # Spot-check failure mode for the balanced cells.
    if (bull, bear) in {(5, 5), (7, 7)}:
        assert failure_mode == "balanced_evidence_no_clear_stance"


def test_decision_table_covers_all_25_grid_cells():
    expected_values = {1, 3, 5, 7, 9}
    assert {b for b, _ in GRID} == expected_values
    assert {c for _, c in GRID} == expected_values
    assert len(GRID) == 25


def test_bull_strength_out_of_range_raises():
    with pytest.raises(ValueError):
        verdict_from_strengths(-1, 5)
    with pytest.raises(ValueError):
        verdict_from_strengths(11, 5)


def test_bear_strength_out_of_range_raises():
    with pytest.raises(ValueError):
        verdict_from_strengths(5, -1)
    with pytest.raises(ValueError):
        verdict_from_strengths(5, 11)
