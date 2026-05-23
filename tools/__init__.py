"""Phase 2 deterministic-arithmetic tools for Claude1.

Per swing-risk-compliance-doctrine Requirement 2: all decision-affecting
arithmetic lives here, never in agent reasoning. Each tool exposes a pure
``compute(...)`` function that returns a :class:`TraceEntry` — the unit the
ledger's ``reasoning_trace`` array stores.
"""
__version__ = "0.1.0"
