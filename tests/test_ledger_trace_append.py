"""Tests for tools.ledger_trace_append — the safe reasoning_trace appender that
prevents the 2026-06-06 hand-edit ledger corruption."""
from __future__ import annotations

import textwrap

import pytest
import yaml

from tools.ledger_trace_append import LedgerAppendError, append_trace


def _entry(tool="tools.regime_check"):
    return {"tool": tool, "inputs": {"ticker": "X"}, "output": {"ok": True},
            "fetched_at": "2026-06-07T00:00:00+00:00"}


def _seed(tmp_path, body):
    p = tmp_path / "X.yml"
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return p


def test_appends_and_assigns_next_id(tmp_path):
    p = _seed(tmp_path, """
        meta:
          state: starter
        reasoning_trace:
          - id: 1
            tool: t1
            output: {}
            fetched_at: "2026-06-06T00:00:00+00:00"
    """)
    new_id = append_trace(p, _entry())
    assert new_id == 2
    doc = yaml.safe_load(p.read_text())
    assert [s["id"] for s in doc["reasoning_trace"]] == [1, 2]
    assert doc["reasoning_trace"][1]["tool"] == "tools.regime_check"


def test_creates_trace_list_when_absent(tmp_path):
    p = _seed(tmp_path, "meta:\n  state: starter\n")
    new_id = append_trace(p, _entry())
    assert new_id == 1
    assert yaml.safe_load(p.read_text())["reasoning_trace"][0]["id"] == 1


def test_result_is_valid_yaml_roundtrip(tmp_path):
    p = _seed(tmp_path, """
        meta:
          state: starter
        notes: >
          a multi-line
          note block
        reasoning_trace:
          - id: 1
            tool: t1
            output: {}
            fetched_at: "2026-06-06T00:00:00+00:00"
    """)
    append_trace(p, _entry())
    # Must still parse cleanly (the corruption mode appended OUTSIDE the list).
    doc = yaml.safe_load(p.read_text())
    assert isinstance(doc["reasoning_trace"], list)
    assert len(doc["reasoning_trace"]) == 2
    assert "multi-line" in doc["notes"]


def test_refuses_corrupt_ledger(tmp_path):
    p = _seed(tmp_path, "meta:\n  state: starter\nnotes: >\n  hi\n- id: 9\n  x: 1\n")
    with pytest.raises(LedgerAppendError, match="already corrupt"):
        append_trace(p, _entry())


def test_refuses_entry_with_id(tmp_path):
    p = _seed(tmp_path, "meta:\n  state: starter\n")
    e = _entry(); e["id"] = 5
    with pytest.raises(LedgerAppendError, match="must NOT carry an id"):
        append_trace(p, e)


def test_refuses_missing_required_keys(tmp_path):
    p = _seed(tmp_path, "meta:\n  state: starter\n")
    with pytest.raises(LedgerAppendError, match="missing required keys"):
        append_trace(p, {"tool": "t"})  # no output / fetched_at


def test_refuses_non_list_reasoning_trace(tmp_path):
    p = _seed(tmp_path, "meta:\n  state: starter\nreasoning_trace: not_a_list\n")
    with pytest.raises(LedgerAppendError, match="expected list"):
        append_trace(p, _entry())


def test_missing_file(tmp_path):
    with pytest.raises(LedgerAppendError, match="not found"):
        append_trace(tmp_path / "nope.yml", _entry())
