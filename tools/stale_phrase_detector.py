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


# Per the doctrine's explicit BLOCK list.
#
# Whitespace handling: patterns use ``\s+`` between tokens instead of a
# literal space so they catch Unicode whitespace (NBSP `` ``, ZWSP,
# narrow no-break space) and soft line breaks. Python 3 ``\s`` matches
# Unicode whitespace by default. This closed an escape vector found in
# the 2026-05-23 red-team pass.
PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        name="as_of_my_training",
        regex=re.compile(
            r"\bas\s+of\s+(?:my\s+training|my\s+last\s+training|my\s+last\s+update|my\s+knowledge\s+cutoff|my\s+data\s+cutoff)\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Model is referencing its training cutoff — implicit stale-data admission.",
    ),
    Pattern(
        name="as_of_late_year",
        regex=re.compile(
            r"\bas\s+of\s+(?:late|early|mid|end\s+of)\s+(?:19|20)\d{2}\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Model is anchoring to a pre-training calendar period.",
    ),
    Pattern(
        name="at_the_time_of_my_data",
        regex=re.compile(
            r"\bat\s+the\s+time\s+of\s+(?:my\s+(?:data|training|knowledge))\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Explicit reference to pre-training data state.",
    ),
    Pattern(
        name="no_realtime_access",
        regex=re.compile(
            r"\b(?:i\s+(?:do\s+not|don'?t|cannot|can'?t)\s+(?:have\s+)?access\s+(?:to\s+)?real[\s-]?time"
            r"|i\s+cannot\s+(?:verify|access)\s+current"
            r"|i'?m\s+not\s+able\s+to\s+(?:access|verify)\s+(?:current|real[\s-]?time))\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Model is hedging that it lacks live data — must come from ledger instead.",
    ),
    Pattern(
        name="i_dont_have_current",
        regex=re.compile(
            r"\bi\s+(?:do\s+not|don'?t)\s+have\s+(?:the\s+)?(?:current|latest|up[-\s]?to[-\s]?date)\s+(?:price|data|figure|number|rate|value)\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Model admits lacking current value — should call a tool, not narrate.",
    ),
    Pattern(
        name="based_on_pre_training",
        regex=re.compile(
            r"\bbased\s+on\s+(?:my\s+)?(?:pre[-\s]?training|training\s+data|historical\s+training)\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Decision-grounding in pre-training data is unfaithful per Requirement 4.",
    ),
    # --- 2026-05-23 red-team additions: paraphrase / memory / knowledge ---
    Pattern(
        name="memory_recall",
        regex=re.compile(
            r"\b(?:based\s+on|from)\s+what\s+i\s+(?:recall|remember|recollect)\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Memory-based recall instead of ledger lookup — implicit stale-data admission.",
    ),
    Pattern(
        name="per_my_memory",
        regex=re.compile(
            r"\bper\s+my\s+(?:memory|recollection|recall)\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Anchoring on personal memory rather than the fact ledger.",
    ),
    Pattern(
        name="knowledge_state_reference",
        regex=re.compile(
            # "my knowledge ends", "my knowledge base reflects", "my training window",
            # "within my training window", etc.
            r"\b(?:my|within\s+my)\s+(?:knowledge|training)(?:\s+base|\s+window|\s+cutoff)?\s+(?:ends|reflects|extends|stops|window|cutoff|covers)\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Explicit reference to model knowledge-state boundary — should call a tool.",
    ),
    Pattern(
        name="within_training_window",
        regex=re.compile(
            r"\bwithin\s+my\s+training\s+window\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Anchoring claims to the pre-training time window.",
    ),
    Pattern(
        name="up_until_my_refresh",
        regex=re.compile(
            r"\bup\s+until\s+my\s+(?:last\s+)?(?:refresh|update|knowledge|training|data)\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Time-bounded admission of stale knowledge state.",
    ),
    Pattern(
        name="without_current_data_phrase",
        regex=re.compile(
            r"\bwithout\s+(?:current|live|recent|up[-\s]?to[-\s]?date|real[\s-]?time)\s+(?:data|prices|info|information|quotes?)\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Model is producing analysis while admitting absence of current data.",
    ),
    Pattern(
        name="probably_temporal_value",
        regex=re.compile(
            # "AAPL was probably near $230 last I checked"
            r"\bprobably\s+(?:near|around|at|about)\s+\$?\d",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Probabilistic numerical claim — values must come from the ledger, not guesses.",
    ),
    Pattern(
        name="last_i_temporal",
        regex=re.compile(
            r"\blast\s+i\s+(?:checked|looked|saw|recall|remember)\b",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Personal-recall temporal hedge — implicit stale-data admission.",
    ),
    Pattern(
        name="historical_vague_price",
        regex=re.compile(
            # "historically AAPL traded around $200" — historical context + vague pricing
            r"\bhistorically\b[^.]{0,80}?(?:near|around|roughly|approximately)\s+\$?\d",
            re.IGNORECASE,
        ),
        severity="BLOCK",
        description="Historical anchoring combined with vague pricing — pre-training narrative.",
    ),
    # --- WARN patterns (downgrade rather than block) ---
    Pattern(
        name="model_speculative_now",
        regex=re.compile(
            r"\b(?:i\s+(?:would|might|may)\s+(?:guess|estimate|assume|presume)|likely\s+(?:around|approximately)\s+\$)\b",
            re.IGNORECASE,
        ),
        severity="WARN",
        description="Speculative phrasing about quantitative values; downgrade rather than block.",
    ),
    Pattern(
        name="give_or_take_estimate",
        regex=re.compile(
            # "Roughly 200, give or take 10" — vague estimation marker; high
            # specificity due to "give or take".
            r"\b(?:roughly|approximately|about)\s+\d[\d,.]*\b.{0,30}?\bgive\s+or\s+take\b",
            re.IGNORECASE,
        ),
        severity="WARN",
        description="Estimation hedge with explicit imprecision marker — quantitative claims must cite tools.",
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
