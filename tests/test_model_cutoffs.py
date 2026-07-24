"""Tests for tools.model_cutoffs — A1 knowledge-cutoff registry.

Registry values are sourced from the vendor model docs (see module docstring);
these tests pin the RESOLUTION MECHANICS (prefix matching, contamination
boundary), not the vendor dates themselves — updating a date in the registry
should only require touching the paired constant here.
"""
from __future__ import annotations

import datetime as _dt

from tools.model_cutoffs import (
    cutoff_for,
    eval_window_contaminated,
    reliable_cutoff_for,
)

# Paired with the registry — update together with tools/model_cutoffs.py.
HAIKU_45_TRAINING_CUTOFF = "2025-07-31"
OPUS_48_TRAINING_CUTOFF = "2026-01-31"


def test_exact_id_resolves():
    assert cutoff_for("claude-opus-4-8") == OPUS_48_TRAINING_CUTOFF


def test_dated_snapshot_resolves_via_prefix():
    assert cutoff_for("claude-haiku-4-5-20251001") == HAIKU_45_TRAINING_CUTOFF


def test_vertex_at_suffix_resolves():
    assert cutoff_for("claude-haiku-4-5@20251001") == HAIKU_45_TRAINING_CUTOFF


def test_longest_prefix_wins():
    # claude-sonnet-4-6 must NOT fall through to a shorter sonnet prefix.
    assert cutoff_for("claude-sonnet-4-6") == "2026-01-31"
    assert cutoff_for("claude-sonnet-4-5-20250929") == "2025-07-31"


def test_unknown_model_returns_none_not_guess():
    assert cutoff_for("claude-imaginary-9") is None
    assert reliable_cutoff_for("gpt-4o") is None
    assert cutoff_for("") is None


def test_training_cutoff_is_the_conservative_gate():
    # Haiku 4.5: reliable Feb 2025, training Jul 2025 — the gate must be
    # the broader training date.
    assert reliable_cutoff_for("claude-haiku-4-5") == "2025-02-28"
    assert cutoff_for("claude-haiku-4-5") == HAIKU_45_TRAINING_CUTOFF


def test_contamination_boundary():
    # Window starting ON the cutoff day is contaminated (<=); the day after
    # is clean.
    assert eval_window_contaminated("claude-haiku-4-5", "2025-07-31") is True
    assert eval_window_contaminated("claude-haiku-4-5", "2025-08-01") is False
    assert eval_window_contaminated("claude-haiku-4-5", _dt.date(2024, 1, 1)) is True


def test_contamination_unknown_model_is_none():
    # Unknown model → None (surface), never a silent False.
    assert eval_window_contaminated("mystery-model", "2020-01-01") is None
