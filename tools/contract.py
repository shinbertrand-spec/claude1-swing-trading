"""Tool I/O contract — :class:`TraceEntry`.

Every Phase 2 tool returns a :class:`TraceEntry`. Callers serialise it into
the ledger's ``reasoning_trace`` array; conclusions cite the entry by its
``id`` field. The shape mirrors ``ledgers/_schema/ledger.schema.json``
``$defs.trace_step``.

Why a dataclass and not a dict: typed shape across tools, single
``to_dict`` / ``to_json`` path so YAML and stdout look the same.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


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
