"""Phase 3 — stale-phrase BLOCK list (per Requirement 4 subtle failure).

Per ``swing-risk-compliance-doctrine.md`` Requirement 4: a model that
writes *"as of my training cutoff"* or *"as of late 2024"* in its analysis
is implicitly admitting the data is stale. ``risk-and-compliance`` must
BLOCK on these phrases — every fact in agent output must come from the
live ledger, not the model's pre-training memory.

This module scans prose (a ``trade-researcher`` Markdown report, a journal
candidate block, etc.) for the forbidden phrases listed in the doctrine.

Patterns are deliberately conservative — we'd rather miss a subtle case
than false-positive on legitimate use. Add patterns as edge cases surface
in the journal review loop.

CLI::

    uv run python -m tools.stale_phrase_detector report.md
    # or pipe:
    echo "as of late 2024, AAPL was at $230" | uv run python -m tools.stale_phrase_detector -
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

from .cli import emit
from .contract import TraceEntry

TOOL = "tools/stale_phrase_detector.py"


@dataclass(frozen=True)
class Pattern:
    name: str
    regex: re.Pattern
    severity: str             # "BLOCK" | "WARN"
    description: str


# Per the doctrine's explicit BLOCK list:
PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        name="as_of_my_training",
        regex=re.compile(
            r"\bas of (?:my training|my last training|my last update|my knowledge cutoff|my data cutoff)\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Model is referencing its training cutoff — implicit stale-data admission.",
    ),
    Pattern(
        name="as_of_late_year",
        regex=re.compile(
            r"\bas of (?:late|early|mid|end of) (?:19|20)\d{2}\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Model is anchoring to a pre-training calendar period.",
    ),
    Pattern(
        name="at_the_time_of_my_data",
        regex=re.compile(
            r"\bat the time of (?:my (?:data|training|knowledge))\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Explicit reference to pre-training data state.",
    ),
    Pattern(
        name="no_realtime_access",
        regex=re.compile(
            r"\b(?:i (?:do not|don'?t|cannot|can'?t) (?:have )?access (?:to )?real[\s-]?time"
            r"|i cannot (?:verify|access) current"
            r"|i'?m not able to (?:access|verify) (?:current|real[\s-]?time))\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Model is hedging that it lacks live data — must come from ledger instead.",
    ),
    Pattern(
        name="i_dont_have_current",
        regex=re.compile(
            r"\bi (?:do not|don'?t) have (?:the )?(?:current|latest|up[-\s]?to[-\s]?date) (?:price|data|figure|number|rate|value)\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Model admits lacking current value — should call a tool, not narrate.",
    ),
    Pattern(
        name="based_on_pre_training",
        regex=re.compile(
            r"\bbased on (?:my )?(?:pre[-\s]?training|training data|historical training)\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Decision-grounding in pre-training data is unfaithful per Requirement 4.",
    ),
    Pattern(
        name="model_speculative_now",
        regex=re.compile(
            r"\b(?:i (?:would|might|may) (?:guess|estimate|assume|presume)|likely (?:around|approximately) \$)\b",
            re.IGNORECASE,
        ),
        severity="WARN",
        description="Speculative phrasing about quantitative values; downgrade rather than block.",
    ),
)


@dataclass(frozen=True)
class Match:
    pattern: str
    severity: str
    description: str
    line: int
    column: int
    span: tuple[int, int]
    matched_text: str


def scan(text: str) -> list[Match]:
    """Return every pattern match in ``text`` in document order."""
    matches: list[Match] = []
    # Pre-compute line offsets for line-number lookups.
    line_offsets = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_offsets.append(i + 1)
    for pattern in PATTERNS:
        for m in pattern.regex.finditer(text):
            start = m.start()
            # Find line number via bisect.
            lo, hi = 0, len(line_offsets) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if line_offsets[mid] <= start:
                    lo = mid
                else:
                    hi = mid - 1
            line = lo + 1
            col = start - line_offsets[lo] + 1
            matches.append(
                Match(
                    pattern=pattern.name,
                    severity=pattern.severity,
                    description=pattern.description,
                    line=line,
                    column=col,
                    span=(m.start(), m.end()),
                    matched_text=m.group(0),
                )
            )
    matches.sort(key=lambda m: m.span[0])
    return matches


def compute(text: str) -> TraceEntry:
    """Wrap :func:`scan` in the :class:`TraceEntry` contract."""
    matches = scan(text)
    blocks = [m for m in matches if m.severity == "BLOCK"]
    warns = [m for m in matches if m.severity == "WARN"]
    return TraceEntry(
        tool=TOOL,
        inputs={"text_chars": len(text), "pattern_count": len(PATTERNS)},
        output={
            "block_count": len(blocks),
            "warn_count": len(warns),
            "should_block": len(blocks) > 0,
            "matches": [
                {
                    "pattern": m.pattern,
                    "severity": m.severity,
                    "description": m.description,
                    "line": m.line,
                    "column": m.column,
                    "matched_text": m.matched_text,
                }
                for m in matches
            ],
        },
    )


class StalePhraseError(RuntimeError):
    """Raised when ``assert_no_stale_phrases`` finds a BLOCK-severity match."""


def assert_no_stale_phrases(text: str) -> TraceEntry:
    """Run :func:`compute`; raise on any BLOCK match. Returns the trace
    entry on success so callers can still inspect WARN matches."""
    entry = compute(text)
    if entry.output["should_block"]:
        first = next(m for m in entry.output["matches"] if m["severity"] == "BLOCK")
        raise StalePhraseError(
            f"BLOCK-severity stale phrase at line {first['line']} col {first['column']}: "
            f"{first['pattern']} — {first['matched_text']!r}. {first['description']}"
        )
    return entry


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.stale_phrase_detector",
        description="Scan prose for Requirement 4 BLOCK-list phrases.",
    )
    p.add_argument(
        "path",
        help="File path to scan. Use '-' to read from stdin.",
    )
    args = p.parse_args()
    if args.path == "-":
        text = sys.stdin.read()
    else:
        with open(args.path, encoding="utf-8") as f:
            text = f.read()
    emit(compute(text))


if __name__ == "__main__":
    main()
