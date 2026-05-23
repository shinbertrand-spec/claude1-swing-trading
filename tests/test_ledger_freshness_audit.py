"""Tests for tools.ledger_freshness_audit."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from tools.ledger_freshness_audit import (
    compute_from_ledger_dict,
    compute_from_path,
)


def _fresh_minimal_ledger(asof: datetime) -> dict:
    return {
        "meta": {
            "schema_version": "1.0",
            "ticker": "TEST",
            "asof": asof.isoformat(timespec="seconds"),
            "state": "candidate",
            "created_by": "test",
            "created_at": asof.isoformat(timespec="seconds"),
        },
        "quote": {
            "last": 100.0, "bid": 99.9, "ask": 100.1, "session": "regular",
            "source": "broker_api",
            "fetched_at": (asof - timedelta(hours=1)).isoformat(timespec="seconds"),
        },
        "technical": {
            "trend_template_passes": 8,
            "computed_at": (asof - timedelta(hours=6)).isoformat(timespec="seconds"),
        },
    }


def test_compute_from_ledger_dict_fresh():
    # Use a known market-hours timestamp (Wed 12 ET).
    asof = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)
    ledger = _fresh_minimal_ledger(asof)
    entry = compute_from_ledger_dict(ledger, asof=asof)
    assert entry.output["overall"] == "fresh"
    assert entry.output["is_fresh"] is True
    assert entry.output["stale_sections"] == []


def test_compute_from_ledger_dict_stale():
    asof = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)
    ledger = _fresh_minimal_ledger(asof)
    # Make technical stale (30h ago).
    ledger["technical"]["computed_at"] = (
        asof - timedelta(hours=30)
    ).isoformat(timespec="seconds")
    entry = compute_from_ledger_dict(ledger, asof=asof)
    assert entry.output["overall"] == "stale"
    assert "technical" in entry.output["stale_sections"]


def test_compute_from_path_round_trips(tmp_path: Path):
    asof = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)
    ledger = _fresh_minimal_ledger(asof)
    yaml_path = tmp_path / "TEST.yml"
    yaml_path.write_text(yaml.safe_dump(ledger), encoding="utf-8")
    entry = compute_from_path(yaml_path, asof=asof)
    assert entry.output["overall"] == "fresh"
    assert entry.inputs["path"] == str(yaml_path)
    assert entry.inputs["ticker"] == "TEST"


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        compute_from_path(tmp_path / "no_such.yml")


def test_subset_sections(tmp_path: Path):
    asof = datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc)
    ledger = _fresh_minimal_ledger(asof)
    # Force technical stale.
    ledger["technical"]["computed_at"] = (
        asof - timedelta(hours=30)
    ).isoformat(timespec="seconds")
    entry = compute_from_ledger_dict(
        ledger, sections=["quote"], asof=asof
    )
    # Only audit quote; technical staleness is invisible.
    assert entry.output["overall"] == "fresh"
    assert all(s["section"] == "quote" for s in entry.output["sections"])


def test_audit_against_example_sepa_vcp_ledger():
    """The packaged example must at least parse and report a coherent
    structure (it's dated, so it may report stale — that's fine)."""
    path = Path("ledgers/_examples/sepa-vcp-candidate.yml")
    if not path.exists():
        pytest.skip(f"example ledger not present at {path}")
    entry = compute_from_path(path)
    assert "overall" in entry.output
    assert "sections" in entry.output
    # The example is from 2026-05-17 so by now it's stale — verify that's
    # what the audit reports, not a crash.
    assert entry.output["overall"] in {"fresh", "stale"}
