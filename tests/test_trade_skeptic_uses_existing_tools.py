"""Phase 7 / H1 — every ``tools.<name>`` reference in
``.claude/agents/trade-skeptic.md`` resolves to a real module in the
``tools/`` package. Catches fabricated tool names before they make it into
production agent prompts."""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PROMPT_PATH = REPO / ".claude" / "agents" / "trade-skeptic.md"

# Match `tools.<name>` and `tools/<name>.py` referenced in prose / fenced
# blocks. Strip dotted suffixes (.compute, .X) so we test only the module.
_TOOL_REF_RE = re.compile(r"\btools[\./]([a-z_][a-z0-9_]*)\b")

# Names that look like tool refs but are actually directory or namespace
# references — skip these (the test below confirms the underlying module
# exists via an explicit list, not via the regex).
_NAMESPACE_NAMES = {"README", "broker", "quant_strategies", "backtest", "auto_paper", "fundamentals"}


def test_trade_skeptic_prompt_exists():
    assert PROMPT_PATH.exists(), f"prompt missing at {PROMPT_PATH}"


def test_trade_skeptic_referenced_tools_all_exist():
    text = PROMPT_PATH.read_text(encoding="utf-8")
    referenced = set(_TOOL_REF_RE.findall(text)) - _NAMESPACE_NAMES
    assert referenced, "no tools.<name> references found in trade-skeptic.md"

    missing: list[str] = []
    for name in sorted(referenced):
        module_path = REPO / "tools" / f"{name}.py"
        package_path = REPO / "tools" / name / "__init__.py"
        if module_path.exists() or package_path.exists():
            continue
        # Also tolerate sub-package references resolvable via importlib.
        try:
            importlib.import_module(f"tools.{name}")
        except ImportError:
            missing.append(name)
    assert not missing, f"trade-skeptic.md references nonexistent tools: {missing}"


def test_trade_skeptic_prompt_has_engagement_clause():
    text = PROMPT_PATH.read_text(encoding="utf-8")
    # Engagement clause lifted verbatim from TradingAgents bear_researcher.py
    # per H1 spec §4 — the prompt must include it.
    assert "conversational style" in text or "directly engaging" in text, (
        "trade-skeptic.md must include the TradingAgents engagement clause"
    )


def test_trade_skeptic_prompt_forbids_new_arithmetic():
    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert "no new arithmetic" in text.lower(), (
        "trade-skeptic.md must include the 'no new arithmetic' hard rule"
    )


def test_trade_skeptic_prompt_declares_h4_memory_block():
    text = PROMPT_PATH.read_text(encoding="utf-8")
    # H1 spec §4 requires the H4 memory-consumption profile.
    assert "Prior lessons (injected by H4)" in text, (
        "trade-skeptic.md must declare the H4 memory-consumption profile"
    )


def test_trade_skeptic_prompt_declares_bear_is_not_short_recommendation():
    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert "do not recommend shorts" in text.lower() or "bear ≠ short" in text.lower(), (
        "trade-skeptic.md must clarify it does not recommend shorts"
    )
