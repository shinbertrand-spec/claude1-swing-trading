"""Phase 4 — extract numerical claims from prose and cross-reference the ledger.

Per ``swing-risk-compliance-doctrine.md`` Requirement 3:

    Conclusion correct but reasoning wrong — most dangerous; lucky alignment
    on this trade, fails on adjacent cases.

The ledger is the source of truth, but agent-produced Markdown is what
Bertrand reads. If the report says "EPS YoY 21%" but the ledger says
"0.0796" (8%), the prose drifted. This module scans report text, extracts
numbers, and flags any quantitative claim that doesn't match a ledger
value within tolerance.

Match strategy is deliberately permissive — false positives are noisy,
false negatives are dangerous. We accept any of:

* Direct ledger-field value match (e.g. ``192.74`` matches ``quote.last``)
* Ledger value times 100 (decimal → percent: ``0.2065`` matches "20.65%")
* Reasoning_trace numeric output values

Bare integers ≤ 100 and dates / IDs are skipped — too many false matches.

CLI::

    uv run python -m tools.claim_extract --report report.md --ledger ledger.yml
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .cli import emit
from .contract import TraceEntry

TOOL = "tools/claim_extract.py"

# Match number forms: 192.74, 1,200,000, 20.65%, $192.74, $1.2B
_NUMBER_RE = re.compile(
    r"""
    (?<![A-Za-z0-9_.])              # boundary
    \$?                              # optional $
    -?                               # optional sign
    \d{1,3}(?:,\d{3})+(?:\.\d+)?     # 1,200,000(.50)
    |
    (?<![A-Za-z0-9_.])
    \$?
    -?\d+(?:\.\d+)?                  # 192.74 or 192
    (?:\s*%)?                        # optional %
    """,
    re.VERBOSE,
)

# Numbers we ignore entirely: years (4-digit), short ints used as ids/counts.
SKIP_INT_RANGE = (0, 100)            # bare integers in [0,100] commonly are
                                      # counts/percentages with ambiguous meaning
DEFAULT_REL_TOL = 1e-3                # 0.1% match tolerance
DEFAULT_ABS_TOL = 1e-6


@dataclass
class Claim:
    raw_text: str
    value: float
    is_percent: bool
    line: int
    column: int
    span: tuple[int, int]
    matched_in_ledger: bool = False
    nearest_field: str = ""
    nearest_value: float | None = None


@dataclass
class ClaimExtractReport:
    claims: list[Claim]
    unmatched: list[Claim] = field(default_factory=list)
    ledger_values: dict[str, float] = field(default_factory=dict)


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            offsets.append(i + 1)
    return offsets


def _line_col(offsets: list[int], pos: int) -> tuple[int, int]:
    lo, hi = 0, len(offsets) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if offsets[mid] <= pos:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1, pos - offsets[lo] + 1


def _parse_number(s: str) -> tuple[float, bool] | None:
    """Return (value, is_percent) or None."""
    text = s.strip()
    is_pct = text.endswith("%")
    text = text.rstrip("%").strip()
    text = text.lstrip("$")
    text = text.replace(",", "")
    try:
        return float(text), is_pct
    except ValueError:
        return None


def _is_year_or_skippable(value: float, is_pct: bool, raw: str) -> bool:
    if is_pct:
        return False
    if "$" in raw or "," in raw or "." in raw:
        return False
    try:
        n = int(value)
    except (TypeError, ValueError):
        return False
    if n != value:
        return False
    if 1900 <= n <= 2200:
        return True
    if SKIP_INT_RANGE[0] <= n <= SKIP_INT_RANGE[1]:
        return True
    return False


def _flatten_ledger(ledger: dict) -> dict[str, float]:
    """Walk the ledger dict; collect every numeric leaf with a dotted path."""
    out: dict[str, float] = {}

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            out[path] = float(node)

    walk(ledger, "")
    return out


def _value_matches(
    claim_value: float, claim_is_pct: bool, ledger_value: float,
    rel: float = DEFAULT_REL_TOL, abs_: float = DEFAULT_ABS_TOL,
) -> bool:
    """Match a claim value against a ledger value, with decimal↔percent
    auto-conversion."""
    candidates = [ledger_value]
    if abs(ledger_value) <= 1.0:
        candidates.append(ledger_value * 100.0)          # decimal → percent
    if abs(ledger_value) > 1.0:
        candidates.append(ledger_value / 100.0)          # percent → decimal
    cv = claim_value
    if claim_is_pct:
        # Claims like "20.65%" carry value=20.65; ledger may store 0.2065 OR 20.65.
        candidates_to_try = candidates
    else:
        candidates_to_try = candidates
    for cand in candidates_to_try:
        if cand == cv:
            return True
        if abs(cv - cand) <= max(abs_, rel * max(abs(cv), abs(cand), 1.0)):
            return True
    return False


def extract(
    text: str,
    ledger: dict,
    rel_tol: float = DEFAULT_REL_TOL,
    abs_tol: float = DEFAULT_ABS_TOL,
) -> ClaimExtractReport:
    """Scan ``text`` for numeric claims; cross-reference against ``ledger``."""
    offsets = _line_offsets(text)
    ledger_values = _flatten_ledger(ledger)
    claims: list[Claim] = []
    for m in _NUMBER_RE.finditer(text):
        raw = m.group(0).strip()
        parsed = _parse_number(raw)
        if parsed is None:
            continue
        value, is_pct = parsed
        if _is_year_or_skippable(value, is_pct, raw):
            continue
        line, col = _line_col(offsets, m.start())
        claim = Claim(
            raw_text=raw,
            value=value,
            is_percent=is_pct,
            line=line,
            column=col,
            span=(m.start(), m.end()),
        )
        # Search ledger for a matching value.
        best_field = ""
        best_value: float | None = None
        for field_path, ledger_value in ledger_values.items():
            if _value_matches(value, is_pct, ledger_value, rel_tol, abs_tol):
                best_field = field_path
                best_value = ledger_value
                break
        if best_field:
            claim.matched_in_ledger = True
            claim.nearest_field = best_field
            claim.nearest_value = best_value
        claims.append(claim)
    unmatched = [c for c in claims if not c.matched_in_ledger]
    return ClaimExtractReport(claims=claims, unmatched=unmatched, ledger_values=ledger_values)


def compute(text: str, ledger: dict) -> TraceEntry:
    report = extract(text, ledger)
    return TraceEntry(
        tool=TOOL,
        inputs={
            "text_chars": len(text),
            "ledger_numeric_fields": len(report.ledger_values),
        },
        output={
            "claim_count": len(report.claims),
            "unmatched_count": len(report.unmatched),
            "should_warn": len(report.unmatched) > 0,
            "claims": [
                {
                    "raw_text": c.raw_text,
                    "value": c.value,
                    "is_percent": c.is_percent,
                    "line": c.line,
                    "column": c.column,
                    "matched_in_ledger": c.matched_in_ledger,
                    "nearest_field": c.nearest_field,
                    "nearest_value": c.nearest_value,
                }
                for c in report.claims
            ],
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.claim_extract",
        description="Extract numeric claims from prose; cross-reference ledger.",
    )
    p.add_argument("--report", required=True, help="Path to prose report (Markdown).")
    p.add_argument("--ledger", required=True, help="Path to ledger YAML.")
    args = p.parse_args()
    text = Path(args.report).read_text(encoding="utf-8")
    ledger = yaml.safe_load(Path(args.ledger).read_text(encoding="utf-8"))
    emit(compute(text, ledger))


if __name__ == "__main__":
    main()
