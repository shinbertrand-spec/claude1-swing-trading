"""Tests for tools.stale_phrase_detector."""
from __future__ import annotations

import pytest

from tools.stale_phrase_detector import (
    StalePhraseError,
    assert_no_stale_phrases,
    scan,
)


def test_detects_as_of_my_training():
    text = "As of my training cutoff, AAPL was trading near $230."
    matches = scan(text)
    assert any(m.pattern == "as_of_my_training" for m in matches)
    assert any(m.severity == "BLOCK" for m in matches)


def test_detects_as_of_late_year():
    text = "As of late 2024, the Fed was cutting rates."
    matches = scan(text)
    assert any(m.pattern == "as_of_late_year" for m in matches)


def test_detects_no_realtime_access():
    text = "I don't have access to real-time prices, but here's my best guess."
    matches = scan(text)
    assert any(m.pattern == "no_realtime_access" for m in matches)


def test_detects_i_dont_have_current():
    text = "I do not have the current price for NVDA at hand."
    matches = scan(text)
    assert any(m.pattern == "i_dont_have_current" for m in matches)


def test_detects_based_on_pre_training():
    text = "Based on my pre-training data, the company was profitable."
    matches = scan(text)
    assert any(m.pattern == "based_on_pre_training" for m in matches)


def test_warn_severity_for_speculative():
    text = "I would estimate the price is likely around $200."
    matches = scan(text)
    warn = [m for m in matches if m.severity == "WARN"]
    assert len(warn) >= 1


def test_clean_text_no_matches():
    text = (
        "AAPL closed at $192.74 per the ledger quote section "
        "fetched at 2026-05-17T14:30:00Z. EPS YoY 20.6% per the "
        "10-Q filed 2026-04-30."
    )
    matches = scan(text)
    assert len(matches) == 0


def test_line_and_column_reporting():
    text = "Line one is clean.\nLine two: as of my training cutoff,\nLine three."
    matches = scan(text)
    assert len(matches) >= 1
    m = next(x for x in matches if x.pattern == "as_of_my_training")
    assert m.line == 2


def test_assert_raises_on_block_match():
    text = "As of my training cutoff, NVDA was at $850."
    with pytest.raises(StalePhraseError, match="BLOCK"):
        assert_no_stale_phrases(text)


def test_assert_passes_on_warn_only():
    text = "I would estimate AAPL near $200."  # WARN, not BLOCK
    entry = assert_no_stale_phrases(text)
    assert entry.output["warn_count"] >= 1
    assert entry.output["should_block"] is False


def test_compute_returns_match_details():
    text = "I cannot verify current prices for AAPL."
    entry = assert_no_stale_phrases.__wrapped__ if hasattr(assert_no_stale_phrases, "__wrapped__") else None
    # Use compute directly via re-import to avoid the assertion's raise path.
    from tools.stale_phrase_detector import compute
    e = compute(text)
    assert e.output["block_count"] >= 1
    assert "matched_text" in e.output["matches"][0]


def test_multiple_matches_sorted_by_position():
    text = (
        "First: as of my training cutoff, NVDA was $850. "
        "Later: I don't have access to real-time data."
    )
    matches = scan(text)
    assert len(matches) >= 2
    for i in range(1, len(matches)):
        assert matches[i].span[0] >= matches[i - 1].span[0]


def test_case_insensitive():
    text = "AS OF MY TRAINING CUTOFF, things were calm."
    matches = scan(text)
    assert any(m.pattern == "as_of_my_training" for m in matches)
