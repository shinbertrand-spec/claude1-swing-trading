"""Auto-paper-specific exceptions for failure-loud invariants.

Per [auto-paper LLM/Python boundary refactor 2026-05-28]: the cron MUST
surface phase-boundary problems rather than silently exit 0. Each exception
maps to a distinct LLM-step recovery action (see
``.claude/commands/auto-paper-v2.md`` for the Telegram-surface contract).
"""
from __future__ import annotations


class AutoPaperError(Exception):
    """Base for auto-paper failure-loud invariants."""


class MissingEnvelopeError(AutoPaperError):
    """A subagent envelope expected by a Python phase is missing from disk.

    Raised by ``run_entry.phase_post_skeptic`` when ANY skeptic envelope is
    absent, and by ``run_entry.phase_post_panel`` when ALL critic envelopes
    for a single ticker are absent.
    """


class OutOfOrderPhaseError(AutoPaperError):
    """A phase was invoked before its prerequisite phase completed.

    Detected via ``_status.yml.last_phase_completed`` not matching the
    expected prerequisite. Indicates a slash-command bug or manual misuse.
    """


class RunDirCorruptError(AutoPaperError):
    """Run directory's status file or expected artifacts are inconsistent.

    Examples: missing ``_status.yml``, ``00_candidates.yml`` shape doesn't
    match ledger schema, run_id in status doesn't match dir name.
    """
