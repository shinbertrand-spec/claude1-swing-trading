"""Safely append a reasoning_trace entry to an existing ledger.

Deterministic appender so sub-agents (notably ``trade-skeptic``) never hand-edit
ledger YAML. Hand-editing is what corrupted ``ledgers/candidates/2026-06-06/
NVDA.yml`` on 2026-06-06: the bear trace entries were written as a bare block
sequence AFTER the ``notes:`` scalar instead of INTO the ``reasoning_trace:``
list, breaking the document so every downstream gate (freshness audit, trace
audit, debate synthesis) failed to load it.

Contract:
  * Parse the ledger (raise loudly if already corrupt -- don't append to a broken
    file).
  * Ensure ``reasoning_trace`` is a list (create it if absent).
  * Assign the next sequential integer id (max existing id + 1; ids start at 1).
  * Append the entry, write back, then RE-PARSE to guarantee the result is still
    valid YAML (fail loud + leave the original untouched if not).

Entry shape mirrors ``tools.contract.TraceEntry`` minus ``id``: keys ``tool``,
``inputs`` (optional), ``output``, ``fetched_at``. The id is assigned here.

  Lib: from tools.ledger_trace_append import append_trace
  CLI: uv run python -m tools.ledger_trace_append <ledger.yml> --entry '<json>'
       (prints the assigned id to stdout)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml


class LedgerAppendError(Exception):
    """Raised when the ledger can't be appended to safely."""


_REQUIRED_KEYS = ("tool", "output", "fetched_at")


def _next_id(trace: list[dict[str, Any]]) -> int:
    max_id = 0
    for step in trace:
        if isinstance(step, dict) and isinstance(step.get("id"), int):
            max_id = max(max_id, step["id"])
    return max_id + 1


def append_trace(ledger_path: str | Path, entry: dict[str, Any]) -> int:
    """Append one reasoning_trace entry to ``ledger_path``; return the new id.

    Raises:
        LedgerAppendError: if the ledger is missing / already-corrupt / not a
            mapping, if the entry is missing required keys, or if the write would
            produce invalid YAML (the original file is left untouched in that case).
    """
    p = Path(ledger_path)
    if not p.is_file():
        raise LedgerAppendError(f"ledger not found: {p}")

    missing = [k for k in _REQUIRED_KEYS if k not in entry]
    if missing:
        raise LedgerAppendError(f"entry missing required keys {missing}: {entry!r}")
    if "id" in entry:
        raise LedgerAppendError("entry must NOT carry an id; append_trace assigns it")

    raw = p.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise LedgerAppendError(
            f"ledger is already corrupt; refusing to append: {p} ({exc})"
        ) from exc
    if not isinstance(doc, dict):
        raise LedgerAppendError(f"ledger top-level is not a mapping: {p}")

    trace = doc.get("reasoning_trace")
    if trace is None:
        trace = []
        doc["reasoning_trace"] = trace
    if not isinstance(trace, list):
        raise LedgerAppendError(
            f"reasoning_trace is {type(trace).__name__}, expected list: {p}"
        )

    new_id = _next_id(trace)
    # Ordered: id first, then the entry fields.
    new_entry = {"id": new_id, **{k: v for k, v in entry.items() if k != "id"}}
    trace.append(new_entry)

    dumped = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    # Guarantee the result is still valid YAML before touching disk.
    try:
        reparsed = yaml.safe_load(dumped)
    except yaml.YAMLError as exc:
        raise LedgerAppendError(f"append would corrupt YAML; aborted: {exc}") from exc
    if not isinstance(reparsed, dict) or not isinstance(
        reparsed.get("reasoning_trace"), list
    ):
        raise LedgerAppendError("post-append validation failed; aborted")

    p.write_text(dumped, encoding="utf-8")
    return new_id


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools.ledger_trace_append")
    ap.add_argument("ledger", help="path to the ledger YAML")
    ap.add_argument("--entry", required=True,
                    help="JSON object with keys tool/inputs?/output/fetched_at (no id)")
    args = ap.parse_args(argv)
    try:
        entry = json.loads(args.entry)
    except json.JSONDecodeError as exc:
        print(f"LEDGER_TRACE_APPEND_FAIL bad --entry JSON: {exc}", flush=True)
        return 1
    try:
        new_id = append_trace(args.ledger, entry)
    except LedgerAppendError as exc:
        print(f"LEDGER_TRACE_APPEND_FAIL {exc}", flush=True)
        return 1
    print(f"LEDGER_TRACE_APPEND_OK id={new_id}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
