"""Tests for tools.claim_extract."""
from __future__ import annotations

from tools.claim_extract import extract


def test_extracts_dollar_value_matched_to_ledger():
    text = "AAPL closed at $192.74 today."
    ledger = {"quote": {"last": 192.74}}
    r = extract(text, ledger)
    assert len(r.claims) == 1
    assert r.claims[0].value == 192.74
    assert r.claims[0].matched_in_ledger is True
    assert r.claims[0].nearest_field == "quote.last"


def test_percent_matches_decimal_ledger_value():
    """'20.65%' should match ledger value 0.2065."""
    text = "EPS YoY growth was 20.65%."
    ledger = {"fundamentals": {"eps_yoy_growth": 0.2065}}
    r = extract(text, ledger)
    pct_claims = [c for c in r.claims if c.is_percent]
    assert len(pct_claims) == 1
    assert pct_claims[0].matched_in_ledger is True


def test_unmatched_value_flagged():
    """Claim with no ledger match goes into unmatched."""
    text = "Position size is $99,999."
    ledger = {"quote": {"last": 100.0}}
    r = extract(text, ledger)
    unmatched_dollars = [c for c in r.unmatched if c.value == 99999.0]
    assert len(unmatched_dollars) == 1


def test_years_are_skipped():
    text = "Earnings filed on 2026-04-30 showed strong growth in 2025."
    ledger = {"meta": {"asof_unused": 1.0}}
    r = extract(text, ledger)
    # No claim with value 2026 or 2025 should be reported.
    for c in r.claims:
        assert int(c.value) != 2026
        assert int(c.value) != 2025


def test_small_integers_skipped():
    text = "We added 8 positions today."
    ledger = {"position_state": {"intended_full_shares": 60}}
    r = extract(text, ledger)
    # 8 is in the skip range [0, 100] for bare ints → should not appear.
    assert all(c.value != 8 for c in r.claims)


def test_comma_separated_thousands():
    text = "Average daily volume: 52,000,000 shares."
    ledger = {"fundamentals": {"avg_daily_volume_shares": 52_000_000}}
    r = extract(text, ledger)
    match = [c for c in r.claims if c.value == 52_000_000]
    assert match and match[0].matched_in_ledger is True


def test_match_within_tolerance():
    """Claim value 0.21 should match ledger 0.2065 within default 0.1%
    tolerance? Actually 0.21 vs 0.2065 = 1.7% diff — should NOT match at default.
    Pin this so we know what the tolerance means."""
    text = "Growth was 21%."
    ledger = {"fundamentals": {"eps_yoy_growth": 0.2065}}
    r = extract(text, ledger)
    # 21 vs 20.65 = ~1.7% diff > 0.1% default — should not match.
    assert all(c.value != 21.0 or not c.matched_in_ledger for c in r.claims) or any(
        c.value == 21.0 and not c.matched_in_ledger for c in r.claims
    )


def test_line_and_column_reported():
    text = "Line 1\nClaim on line 2: $192.74\nLine 3"
    ledger = {"quote": {"last": 192.74}}
    r = extract(text, ledger)
    c = next(c for c in r.claims if c.value == 192.74)
    assert c.line == 2
