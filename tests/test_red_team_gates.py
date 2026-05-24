"""Red-team the 5-gate hallucination-prevention sequence.

These tests deliberately probe whether each deterministic gate catches the
failure modes it was designed to catch. Every test asserts the *expected
protective behavior* — a FAILing test here means the gate has a leak.

Coverage:
* Gate 1 — ``ledger_freshness_audit`` (Requirement 4 staleness)
* Gate 2 — ``trace_audit`` (Requirement 3 unfaithful-reasoning)
* Gate 3 — ``stale_phrase_detector`` (Requirement 4 stale-data hedges)

Gate 4 (hard-rule compliance) and Gate 5 (LLM adversarial review) are
LLM-orchestrated and live outside this harness.

Naming convention: ``test_leak_<gate>_<vector>`` for attacks expected to
slip through the current implementation; ``test_hit_<gate>_<vector>`` for
attacks expected to be caught. Both must run; the leak tests are how we
track the gap. When a leak is closed, flip its name to ``test_hit_*``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tools.freshness import audit_ledger
from tools.stale_phrase_detector import scan
from tools.trace_audit import audit as trace_audit


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

ASOF = datetime(2026, 5, 18, 14, 30, 0, tzinfo=timezone.utc)  # Mon 10:30 ET


def _fresh_quote(asof: datetime = ASOF) -> dict:
    return {
        "last": 192.74,
        "bid": 192.71,
        "ask": 192.77,
        "session": "regular",
        "fetched_at": asof.isoformat(),
    }


def _fresh_technical(asof: datetime = ASOF) -> dict:
    return {
        "trend_template_passes": 8,
        "stage": 2,
        "computed_at": asof.isoformat(),
    }


def _fresh_fundamentals(asof: datetime = ASOF) -> dict:
    return {
        "eps_last_q": 1.87,
        "revenue_last_q": 89_500_000_000,
        "filing_date": (asof.date() - timedelta(days=20)).isoformat(),
        "next_earnings_date": (asof.date() + timedelta(days=45)).isoformat(),
        "next_earnings_source_secondary": "https://nasdaq.com/...",
        "fetched_at": asof.isoformat(),
    }


def _minimal_pure_trace_step(step_id: int) -> dict:
    """A pure-arithmetic trace step that re-runs cleanly."""
    return {
        "id": step_id,
        "tool": "tools/compute_yoy.py",
        "inputs": {"current": 1.87, "prior": 1.55},
        "output": {
            "yoy_growth_decimal": 0.2064516129032259,
            "yoy_growth_pct": 20.64516129032259,
        },
        "fetched_at": ASOF.isoformat(),
    }


# ======================================================================
# Gate 3 — stale_phrase_detector paraphrase attacks
# ======================================================================
#
# The detector's docstring explicitly says: *"Add patterns as edge cases
# surface in the journal review loop."* These are the journal-review-loop
# cases. Each tests an adversarial paraphrase that defeats the existing
# 6 BLOCK patterns + 1 WARN pattern.


@pytest.mark.parametrize(
    "vector, attack_text, expected_severity",
    [
        # BLOCK-severity paraphrase attacks: explicit stale-data admissions.
        ("memory_recall_no_temporal",
         "Based on what I recall, NVDA was trading near $850.",
         "BLOCK"),
        ("memory_remember",
         "From what I remember, the company beat earnings last quarter.",
         "BLOCK"),
        ("knowledge_ends_phrase",
         "My knowledge ends around mid-2025, so I'll estimate the price.",
         "BLOCK"),
        ("up_until_last_refresh",
         "Up until my last refresh, the stock was around $300.",
         "BLOCK"),
        ("without_current_data",
         "Without current data, I'd put NVDA near $250.",
         "BLOCK"),
        ("probably_temporal_hedge",
         "AAPL was probably near $230 last I checked.",
         "BLOCK"),
        ("historically_speculation",
         "Historically, AAPL traded around $200 give or take.",
         "BLOCK"),
        ("memory_of_similar_stocks",
         "Per my memory of similar stocks, this typically rallies 10%.",
         "BLOCK"),
        ("training_window",
         "Within my training window, this stock was in a Stage 4 decline.",
         "BLOCK"),
        ("knowledge_base_phrase",
         "My knowledge base reflects data through early 2025.",
         "BLOCK"),
        # WARN-severity attacks: explicit imprecision but no stale-data
        # admission. The doctrine prefers conservative blocking — these
        # surface as WARN and the prose-number cross-reference in
        # claim_extract is the complementary gate for unsourced numerics.
        ("roughly_no_dollar_anchor",
         "Roughly 200, give or take 10.",
         "WARN"),
    ],
)
def test_hit_gate3_paraphrase(
    vector: str, attack_text: str, expected_severity: str
) -> None:
    """Paraphrase attacks that the original 6-pattern detector missed.

    Each text is either an explicit stale-data admission (BLOCK) or a
    quantitative-imprecision hedge (WARN). A *firing* gate is the bar —
    silent passage on any of these is the failure mode the red-team is
    designed to expose.
    """
    matches = scan(attack_text)
    firing = [m for m in matches if m.severity == expected_severity]
    assert firing, (
        f"vector={vector!r}: detector should have produced a {expected_severity} "
        f"match. Text was {attack_text!r}. "
        f"Matches: {[(m.pattern, m.severity) for m in matches]}"
    )


def test_hit_gate3_nonbreaking_space() -> None:
    """The regex uses literal ASCII space; a non-breaking space (U+00A0)
    between 'as' and 'of' defeats the pattern.

    A model that produces NBSP-laden output (some terminals / copy-paste
    paths inject these) gets a free pass.
    """
    attack = "as of my training cutoff, AAPL was at $230."
    matches = scan(attack)
    blocks = [m for m in matches if m.severity == "BLOCK"]
    assert blocks, (
        f"NBSP attack passed: {[(m.pattern, m.severity) for m in matches]}"
    )


def test_hit_gate3_line_break_split() -> None:
    """The regex doesn't span newlines. Splitting 'as of my training' across
    a soft line break defeats the pattern."""
    attack = "as of\nmy training cutoff, the price was elsewhere."
    matches = scan(attack)
    blocks = [m for m in matches if m.severity == "BLOCK"]
    assert blocks, (
        f"newline-split attack passed: {[(m.pattern, m.severity) for m in matches]}"
    )


def test_hit_gate3_clean_text() -> None:
    """Control: properly-sourced prose should produce zero matches."""
    text = (
        "AAPL closed at $192.74 per ledger.quote.last fetched at "
        "2026-05-17T14:30:00Z. EPS YoY 20.6% per the 10-Q filed 2026-04-30."
    )
    assert scan(text) == [], "clean ledger-cited prose should not match"


# ======================================================================
# Gate 2 — trace_audit structural attacks
# ======================================================================


def test_hit_gate2_empty_trace_refs_on_setup() -> None:
    """Load-bearing setup_classification with empty trace_refs → BLOCK."""
    ledger = {
        "reasoning_trace": [_minimal_pure_trace_step(1)],
        "setup_classification": {
            "type": "SEPA-VCP",
            "trace_refs": [],
        },
    }
    out = trace_audit(ledger)
    assert out["verdict"]["overall"] == "BLOCK", out


def test_hit_gate2_dangling_trace_ref() -> None:
    """trace_refs pointing to a non-existent id → BLOCK."""
    ledger = {
        "reasoning_trace": [_minimal_pure_trace_step(1)],
        "setup_classification": {
            "type": "SEPA-VCP",
            "trace_refs": [99],  # no step with id=99
        },
    }
    out = trace_audit(ledger)
    assert out["verdict"]["overall"] == "BLOCK", out


def test_hit_gate2_divergent_rerun() -> None:
    """A cited PURE step whose recorded output diverges from a re-run → BLOCK."""
    ledger = {
        "reasoning_trace": [
            {
                "id": 1,
                "tool": "tools/compute_yoy.py",
                "inputs": {"current": 1.87, "prior": 1.55},
                "output": {"yoy_decimal": 0.99, "yoy_pct": 99.0},  # fabricated
                "fetched_at": ASOF.isoformat(),
            }
        ],
        "setup_classification": {"type": "SEPA-VCP", "trace_refs": [1]},
    }
    out = trace_audit(ledger)
    assert out["verdict"]["overall"] == "BLOCK", out
    assert any("rerun:" in r for r in out["verdict"]["block_reasons"]), out


def test_hit_gate2_load_bearing_section_entirely_missing() -> None:
    """If setup_classification is completely absent, trace_validate has
    nothing to check — the ledger silently passes.

    Attack: a researcher who simply omits the load-bearing section produces
    a ledger that passes Gate 2. There's no minimum-section requirement.
    """
    ledger = {
        "reasoning_trace": [_minimal_pure_trace_step(1)],
        # setup_classification deliberately absent
    }
    out = trace_audit(ledger)
    assert out["verdict"]["overall"] == "BLOCK", (
        "ledger without setup_classification should not pass Gate 2 — "
        f"got: {out['verdict']}"
    )


def test_hit_gate2_confluence_status_unknown_bypasses_trace_req() -> None:
    """confluence_checklist items with status=UNKNOWN skip the trace_refs
    requirement (only PASS/FAIL/PARTIAL trigger validation).

    Attack: a researcher marks every load-bearing criterion as UNKNOWN to
    bypass the trace requirement, then makes the final classification on
    no evidence. The validator currently allows this.
    """
    ledger = {
        "reasoning_trace": [_minimal_pure_trace_step(1)],
        "setup_classification": {
            "type": "SEPA-VCP",
            "trace_refs": [1],
            "confluence_checklist": [
                {"criterion": "stage_2", "status": "UNKNOWN"},
                {"criterion": "vcp_pattern", "status": "UNKNOWN"},
                {"criterion": "volume_confirm", "status": "UNKNOWN"},
            ],
        },
    }
    out = trace_audit(ledger)
    # We want: at least a WARN, ideally a BLOCK, when an entire checklist
    # is UNKNOWN (the agent is making a load-bearing call on no evidence).
    has_signal = (
        out["verdict"]["overall"] == "BLOCK"
        or any("UNKNOWN" in r.upper() or "unknown" in r for r in out["verdict"]["warn_reasons"])
    )
    assert has_signal, (
        "all-UNKNOWN confluence checklist should not pass silently. "
        f"verdict={out['verdict']}"
    )


def test_hit_gate2_confluence_checklist_wrong_type() -> None:
    """If confluence_checklist is a string instead of a list, the validator's
    isinstance check returns silently (line 209 of trace_validate.py).

    Attack: malformed ledger that bypasses the checklist validator entirely.
    """
    ledger = {
        "reasoning_trace": [_minimal_pure_trace_step(1)],
        "setup_classification": {
            "type": "SEPA-VCP",
            "trace_refs": [1],
            "confluence_checklist": "see the prose report",  # wrong type
        },
    }
    out = trace_audit(ledger)
    assert out["verdict"]["overall"] == "BLOCK", (
        "confluence_checklist as a string should be a structural failure; "
        f"got: {out['verdict']}"
    )


def test_hit_gate2_clean_minimal_ledger() -> None:
    """Control: minimal ledger with one cited pure step should pass."""
    ledger = {
        "reasoning_trace": [_minimal_pure_trace_step(1)],
        "setup_classification": {
            "type": "SEPA-VCP",
            "trace_refs": [1],
        },
    }
    out = trace_audit(ledger)
    assert out["verdict"]["overall"] == "APPROVE", out


# ======================================================================
# Gate 1 — ledger_freshness_audit missing-data attacks
# ======================================================================


def test_hit_gate1_stale_quote_market_hours() -> None:
    """Quote 8 hours old during market hours → stale."""
    ledger = {
        "quote": _fresh_quote(asof=ASOF - timedelta(hours=8)),
    }
    report = audit_ledger(ledger, asof=ASOF)
    assert report.overall == "stale", report


def test_hit_gate1_stale_technical_25h() -> None:
    """Technical computed 25h ago (past the 24h window) → stale."""
    ledger = {
        "technical": _fresh_technical(asof=ASOF - timedelta(hours=25)),
    }
    report = audit_ledger(ledger, asof=ASOF)
    assert report.overall == "stale", report


def test_hit_gate1_missing_quote_section() -> None:
    """A ledger that simply omits the quote section passes the freshness
    audit — audit_ledger filters to sections present in the ledger.

    Attack: drop the most-volatile section (quote) entirely; everything
    else passes; trade is approved on no live price.
    """
    ledger = {
        "technical": _fresh_technical(),
        "fundamentals": _fresh_fundamentals(),
        # quote deliberately absent
    }
    report = audit_ledger(ledger, asof=ASOF)
    # We want either overall=="stale" OR an explicit missing-section warning
    # in the report. Right now there isn't one — overall is just "fresh".
    quote_section_present = any(s.section == "quote" for s in report.sections)
    assert quote_section_present and report.overall == "stale", (
        "missing quote section should be flagged stale or trigger a missing-"
        f"section warning. report.overall={report.overall}, sections="
        f"{[(s.section, s.status) for s in report.sections]}"
    )


def test_hit_gate1_quote_missing_timestamp() -> None:
    """A quote section present but with no `fetched_at` returns
    status='missing_timestamp' — which is NOT 'stale', so overall stays
    'fresh'.

    Attack: present a quote with no timestamp; the gate says fresh.
    """
    ledger = {
        "quote": {
            "last": 192.74,
            "bid": 192.71,
            "ask": 192.77,
            "session": "regular",
            # fetched_at deliberately absent
        },
    }
    report = audit_ledger(ledger, asof=ASOF)
    assert report.overall == "stale", (
        "quote with no fetched_at should not be 'fresh'. "
        f"sections={[(s.section, s.status) for s in report.sections]}"
    )


def test_hit_gate1_technical_missing_computed_at() -> None:
    """Same pattern as the quote case — technical without computed_at
    returns missing_timestamp, which isn't classified as stale."""
    ledger = {
        "technical": {
            "trend_template_passes": 8,
            "stage": 2,
            # computed_at deliberately absent
        },
    }
    report = audit_ledger(ledger, asof=ASOF)
    assert report.overall == "stale", (
        "technical with no computed_at should not be 'fresh'. "
        f"sections={[(s.section, s.status) for s in report.sections]}"
    )


def test_hit_gate1_earnings_blackout_warns() -> None:
    """Earnings within 10 trading days → fundamentals returns a warning
    (but section is still fresh; this is by design — the hard-rule check
    lives in earnings_calendar / position_sizer)."""
    near_earnings = _fresh_fundamentals()
    near_earnings["next_earnings_date"] = (ASOF.date() + timedelta(days=5)).isoformat()
    ledger = {"fundamentals": near_earnings}
    report = audit_ledger(ledger, asof=ASOF)
    fundamentals_section = next(s for s in report.sections if s.section == "fundamentals")
    assert any(
        "next_earnings_date" in w for w in fundamentals_section.warnings
    ), fundamentals_section.warnings
