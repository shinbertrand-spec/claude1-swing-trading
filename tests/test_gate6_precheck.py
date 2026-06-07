"""Gate 0 (doctrine precheck) — verify Gate 6 preconditions before any
SwingVerdict can be composed.

Doctrine context: every SwingVerdict must come from BOTH a bull (trade-
researcher) and a bear (trade-skeptic) case. Historically the skeptic was
skipped, leaving Gate 6 unrunnable and the verdict doctrine-non-compliant.
``gate6_precheck`` enforces the precondition mechanically; the CLI exits
with code 1 when blocked so a Bash-driven gate sequence short-circuits.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tools.debate_synthesis import gate6_precheck


# Minimal valid bear-JSON fragment — matches the BearCase contract closely
# enough for ``parse_bear_json_fragment`` to succeed downstream.
_BEAR_JSON_OK = """
Bear analysis preamble.

```json
{
  "verdict": "INVALIDATION_PARTIAL",
  "thesis_one_sentence": "Stop is too wide for the regime.",
  "risk_triggers": [],
  "bull_counterpoints": [],
  "trace_refs": []
}
```
""".strip()

_BULL_MD_OK = "Bull report — grade B, all gates pass.\n"

_CANDIDATE_LEDGER_MINIMAL = {
    "meta": {
        "ticker": "TEST",
        "ledger_path": "ledgers/candidates/2026-06-07/TEST.yml",
    },
    "setup_classification": {
        "type": "SEPA-VCP",
        "grade": "B",
        "confluence_checklist": [],
    },
}


@pytest.fixture
def candidate_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ledgers" / "candidates" / "2026-06-07"
    d.mkdir(parents=True)
    return d


def _write_ledger(candidate_dir: Path, ticker: str = "TEST") -> Path:
    p = candidate_dir / f"{ticker}.yml"
    p.write_text(yaml.safe_dump(_CANDIDATE_LEDGER_MINIMAL), encoding="utf-8")
    return p


def test_precheck_all_present_can_proceed(candidate_dir: Path) -> None:
    ledger = _write_ledger(candidate_dir)
    (candidate_dir / "TEST.md").write_text(_BULL_MD_OK, encoding="utf-8")
    (candidate_dir / "TEST-bear.md").write_text(_BEAR_JSON_OK, encoding="utf-8")

    result = gate6_precheck(ledger)

    assert result["can_proceed"] is True
    assert result["blockers"] == []
    assert result["bull_report_path"] == str(candidate_dir / "TEST.md")
    assert result["bear_report_path"] == str(candidate_dir / "TEST-bear.md")


def test_precheck_no_bear_blocks(candidate_dir: Path) -> None:
    """The most common doctrine violation: bull exists but skeptic was skipped."""
    ledger = _write_ledger(candidate_dir)
    (candidate_dir / "TEST.md").write_text(_BULL_MD_OK, encoding="utf-8")

    result = gate6_precheck(ledger)

    assert result["can_proceed"] is False
    assert result["bear_report_path"] is None
    assert result["bull_report_path"] == str(candidate_dir / "TEST.md")
    assert any("bear report (trade-skeptic) missing" in b for b in result["blockers"])
    assert any("Invoke trade-skeptic" in b for b in result["blockers"])


def test_precheck_no_bull_no_bear_blocks(candidate_dir: Path) -> None:
    ledger = _write_ledger(candidate_dir)

    result = gate6_precheck(ledger)

    assert result["can_proceed"] is False
    assert len(result["blockers"]) == 2
    assert any("bull report" in b for b in result["blockers"])
    assert any("bear report" in b for b in result["blockers"])


def test_precheck_bear_without_json_fragment_blocks(candidate_dir: Path) -> None:
    """Bear ran but skipped the structured contract — equally non-compliant."""
    ledger = _write_ledger(candidate_dir)
    (candidate_dir / "TEST.md").write_text(_BULL_MD_OK, encoding="utf-8")
    (candidate_dir / "TEST-bear.md").write_text(
        "Bear report with prose but no terminal JSON fragment.\n",
        encoding="utf-8",
    )

    result = gate6_precheck(ledger)

    assert result["can_proceed"] is False
    assert any("terminal ```json fenced block" in b for b in result["blockers"])


def test_precheck_missing_ledger_blocks(tmp_path: Path) -> None:
    ledger = tmp_path / "ledgers" / "candidates" / "2026-06-07" / "MISSING.yml"

    result = gate6_precheck(ledger)

    assert result["can_proceed"] is False
    assert any("candidate ledger missing" in b for b in result["blockers"])


def test_precheck_cli_exits_1_when_blocked(candidate_dir: Path) -> None:
    """CLI contract: exit code 1 when precheck blocks; risk-and-compliance
    Gate 0 step depends on this for Bash short-circuiting."""
    ledger = _write_ledger(candidate_dir)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.debate_synthesis",
            "--precheck",
            str(ledger),
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent),
    )

    assert proc.returncode == 1, f"expected exit=1, got {proc.returncode}; stdout={proc.stdout!r}"
    payload = json.loads(proc.stdout)
    assert payload["can_proceed"] is False
    assert len(payload["blockers"]) >= 1


def test_precheck_cli_exits_0_when_ready(candidate_dir: Path) -> None:
    ledger = _write_ledger(candidate_dir)
    (candidate_dir / "TEST.md").write_text(_BULL_MD_OK, encoding="utf-8")
    (candidate_dir / "TEST-bear.md").write_text(_BEAR_JSON_OK, encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.debate_synthesis",
            "--precheck",
            str(ledger),
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent),
    )

    assert proc.returncode == 0, f"expected exit=0, got {proc.returncode}; stdout={proc.stdout!r}; stderr={proc.stderr!r}"
    payload = json.loads(proc.stdout)
    assert payload["can_proceed"] is True


def test_precheck_cli_requires_bull_bear_without_precheck_flag(candidate_dir: Path) -> None:
    """Backward compat: invocation without --precheck still requires --bull + --bear.
    Previously these were `required=True` on argparse; now they're conditional."""
    ledger = _write_ledger(candidate_dir)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.debate_synthesis",
            str(ledger),
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent),
    )

    assert proc.returncode != 0
    assert "--bull and --bear are required" in proc.stderr
