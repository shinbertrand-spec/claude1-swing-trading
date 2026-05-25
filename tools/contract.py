"""Tool I/O contract — :class:`TraceEntry` plus Phase 7 debate models.

Every Phase 2 tool returns a :class:`TraceEntry`. Callers serialise it into
the ledger's ``reasoning_trace`` array; conclusions cite the entry by its
``id`` field. The shape mirrors ``ledgers/_schema/ledger.schema.json``
``$defs.trace_step``.

Why a dataclass and not a dict: typed shape across tools, single
``to_dict`` / ``to_json`` path so YAML and stdout look the same.

Phase 7 (multi-agent debate, H1) adds the pydantic models that
``tools.debate_synthesis`` composes and ``ledgers/debate/`` ledgers store:
:class:`SwingVerdict` (stub — H3 owns the canonical definition),
:class:`BullCase`, :class:`BearCase`, :class:`SynthesisResult`, and
:class:`DebateState`. These mirror ``ledgers/debate/_schema/debate.schema.json``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class TraceEntry:
    """One numbered tool-output entry in a ledger ``reasoning_trace``.

    Attributes:
        tool: e.g. ``"tools/trend_template.py"``. Used by Phase 4 verification
            to re-run the same tool against the same inputs.
        inputs: keyword arguments the tool was called with. Must be
            JSON-serialisable so the trace is re-runnable.
        output: tool result. Any JSON-serialisable shape.
        fetched_at: ISO-8601 UTC timestamp at the moment the output was
            produced. Per Requirement 4 (temporal context awareness).
        id: assigned when appended to a ledger; ``None`` until then.
    """

    tool: str
    inputs: dict[str, Any]
    output: Any
    fetched_at: str = field(default_factory=_utc_now_iso)
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d["id"] is None:
            d.pop("id")
        return d

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


# ---------------------------------------------------------------------------
# Phase 7 — multi-agent debate (H1) pydantic models
#
# Mirror ``ledgers/debate/_schema/debate.schema.json``. ``SwingVerdict`` is
# stubbed here per the H1 spec §3 interface note: H3 owns the canonical
# enum; H1 imports it. When H3 ships its pydantic enum, replace this stub
# in place — the value names are committed.
# ---------------------------------------------------------------------------


class SwingVerdict(str, Enum):
    """H3 5-way verdict. TODO H3: replace this stub with the canonical enum
    once the H3 design spec ships. Value names are committed per the H1 spec.
    """

    ENTRY_STRONG = "ENTRY_STRONG"
    ENTRY_NORMAL = "ENTRY_NORMAL"
    WATCH_BUILD_THESIS = "WATCH_BUILD_THESIS"
    DEFER = "DEFER"
    REJECT = "REJECT"


class BearVerdict(str, Enum):
    """Bear-case sub-verdict emitted by ``trade-skeptic``. Distinct from
    :class:`SwingVerdict` — this is the bear's own conviction, not the
    final synthesis."""

    INVALIDATION_STRONG = "INVALIDATION_STRONG"
    INVALIDATION_PARTIAL = "INVALIDATION_PARTIAL"
    INVALIDATION_WEAK = "INVALIDATION_WEAK"


class DebateMode(str, Enum):
    """Whether the debate ran in single-shot DAG mode or multi-turn
    conversational mode. v1 commits to ``single_shot`` per H1 spec §5; the
    enum exists so a future v2 can flip without a breaking schema change."""

    SINGLE_SHOT = "single_shot"
    MULTI_TURN = "multi_turn"


class BullCase(BaseModel):
    """Bull-side debate input — sourced from the existing candidate ledger
    + the trade-researcher Markdown report."""

    model_config = ConfigDict(extra="forbid")

    report_path: str
    thesis_one_sentence: str
    confluence_grade: str | None = None  # A+/A/B/C/null per setup_classification
    trace_refs: list[int] = Field(default_factory=list)


class BullCounterpoint(BaseModel):
    """One bear engagement with a specific bull claim."""

    model_config = ConfigDict(extra="forbid")

    bull_claim_quoted: str
    counter_evidence: str
    trace_refs: list[int] = Field(default_factory=list)


class RiskTrigger(BaseModel):
    """One mechanically-testable invalidation condition."""

    model_config = ConfigDict(extra="forbid")

    condition: str
    trace_refs: list[int] = Field(default_factory=list)
    already_fired: bool = False  # True if the condition is already met at debate time


class BearCase(BaseModel):
    """Bear-side debate input — sourced from the trade-skeptic Markdown
    report's terminal JSON fragment."""

    model_config = ConfigDict(extra="forbid")

    report_path: str
    verdict: BearVerdict
    risk_triggers: list[RiskTrigger] = Field(default_factory=list)
    bull_counterpoints: list[BullCounterpoint] = Field(default_factory=list)
    trace_refs: list[int] = Field(default_factory=list)


class SynthesisResult(BaseModel):
    """Gate 6 output — the facilitator's typed verdict and rationale."""

    model_config = ConfigDict(extra="forbid")

    facilitator: str = "risk-and-compliance"
    verdict: SwingVerdict
    rationale_one_paragraph: str
    bull_strength_score: int = Field(ge=0, le=10)
    bear_strength_score: int = Field(ge=0, le=10)
    decisive_evidence_trace_refs: list[int] = Field(default_factory=list)
    failure_mode: str | None = None  # e.g. "balanced_evidence_no_clear_stance"
    computed_at: str
    tool: str = "tools/debate_synthesis.py"


class DebateState(BaseModel):
    """Top-level debate ledger object. Mirrors
    ``ledgers/debate/_schema/debate.schema.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    ticker: str
    date: str  # ISO date
    candidate_ledger_path: str
    bull_case: BullCase
    bear_case: BearCase
    debate_mode: DebateMode = DebateMode.SINGLE_SHOT
    synthesis: SynthesisResult
