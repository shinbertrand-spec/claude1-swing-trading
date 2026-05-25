"""Phase 7 / H1 — pydantic models roundtrip losslessly through .model_dump() /
.model_validate(). Schema-driven; this is the contract H3 will refine later."""
from __future__ import annotations

from tools.contract import (
    BearCase,
    BearVerdict,
    BullCase,
    BullCounterpoint,
    DebateMode,
    DebateState,
    RiskTrigger,
    SwingVerdict,
    SynthesisResult,
)


def _sample_debate_state() -> DebateState:
    return DebateState(
        schema_version="1.0",
        ticker="CEG",
        date="2026-05-25",
        candidate_ledger_path="ledgers/candidates/2026-05-25/CEG.yml",
        bull_case=BullCase(
            report_path="ledgers/candidates/2026-05-25/CEG.md",
            thesis_one_sentence="Stage-2 SEPA-VCP on EPS YoY acceleration with regime tailwind.",
            confluence_grade="A",
            trace_refs=[3, 7, 12, 18],
        ),
        bear_case=BearCase(
            report_path="ledgers/candidates/2026-05-25/CEG-bear.md",
            verdict=BearVerdict.INVALIDATION_PARTIAL,
            risk_triggers=[
                RiskTrigger(
                    condition="Close below $245",
                    trace_refs=[21],
                    already_fired=False,
                ),
            ],
            bull_counterpoints=[
                BullCounterpoint(
                    bull_claim_quoted="Sector qualifies",
                    counter_evidence="Score borderline at 5/7",
                    trace_refs=[22],
                ),
            ],
            trace_refs=[21, 22],
        ),
        debate_mode=DebateMode.SINGLE_SHOT,
        synthesis=SynthesisResult(
            facilitator="risk-and-compliance",
            verdict=SwingVerdict.ENTRY_NORMAL,
            rationale_one_paragraph="Bull A grade, bear partial, gap clear.",
            bull_strength_score=7,
            bear_strength_score=4,
            decisive_evidence_trace_refs=[3, 7, 22],
            failure_mode=None,
            computed_at="2026-05-25T14:15:00+00:00",
            tool="tools/debate_synthesis.py",
        ),
    )


def test_debate_state_roundtrips_via_model_dump():
    original = _sample_debate_state()
    dumped = original.model_dump(mode="json")
    rehydrated = DebateState.model_validate(dumped)
    assert rehydrated == original
    assert rehydrated.model_dump(mode="json") == dumped


def test_bull_case_roundtrip():
    bc = BullCase(
        report_path="x.md",
        thesis_one_sentence="thesis",
        confluence_grade="A+",
        trace_refs=[1, 2, 3],
    )
    assert BullCase.model_validate(bc.model_dump()) == bc


def test_bear_case_roundtrip_with_already_fired_trigger():
    bc = BearCase(
        report_path="x-bear.md",
        verdict=BearVerdict.INVALIDATION_STRONG,
        risk_triggers=[
            RiskTrigger(condition="Stop already broken", trace_refs=[5], already_fired=True),
        ],
        bull_counterpoints=[],
        trace_refs=[5],
    )
    dumped = bc.model_dump()
    rehydrated = BearCase.model_validate(dumped)
    assert rehydrated == bc
    assert rehydrated.risk_triggers[0].already_fired is True


def test_synthesis_result_roundtrip_with_failure_mode():
    sr = SynthesisResult(
        verdict=SwingVerdict.WATCH_BUILD_THESIS,
        rationale_one_paragraph="balanced",
        bull_strength_score=5,
        bear_strength_score=5,
        decisive_evidence_trace_refs=[1],
        failure_mode="balanced_evidence_no_clear_stance",
        computed_at="2026-05-25T14:15:00+00:00",
    )
    assert SynthesisResult.model_validate(sr.model_dump()) == sr


def test_swing_verdict_enum_has_5_values():
    # H3 ownership: names committed.
    assert {v.value for v in SwingVerdict} == {
        "ENTRY_STRONG",
        "ENTRY_NORMAL",
        "WATCH_BUILD_THESIS",
        "DEFER",
        "REJECT",
    }


def test_swing_verdict_enum_values_serialize_as_strings():
    sr = SynthesisResult(
        verdict=SwingVerdict.ENTRY_STRONG,
        rationale_one_paragraph="r",
        bull_strength_score=9,
        bear_strength_score=2,
        decisive_evidence_trace_refs=[],
        computed_at="2026-05-25T14:15:00+00:00",
    )
    dumped = sr.model_dump(mode="json")
    assert dumped["verdict"] == "ENTRY_STRONG"
