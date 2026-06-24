"""Tests for tools.cluster_calibration — the append-only cluster-cap sink."""
from __future__ import annotations

import json
from datetime import date

from tools import cluster_calibration
from tools.cluster_calibration import append_decision

# A representative compute() output dict (the half_size discretionary case).
_OUTPUT = {
    "theme": "AI-momentum", "track": "discretionary", "action": "half_size",
    "breach": True, "ceiling_breach": False, "cluster_pct": 0.27,
    "effective_cap_pct": 0.25, "hard_ceiling_pct": 0.45,
    "regime_class": "stage_2_weakening", "binding_constraint": "theme_cluster_cap",
    "cluster_total_usd": 27_000.0, "proposed_cost_usd": 5_000.0,
    "existing_same_cluster_cost_usd": 22_000.0,
}


def _read(path):
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_append_decision_writes_wellformed_line(tmp_path):
    path = append_decision(
        output=_OUTPUT, fetched_at="2026-06-24T12:00:00+00:00",
        ticker="NVDA", source="discretionary-cli",
        ledger_date=date(2026, 6, 24), calib_dir=tmp_path,
    )
    assert path == tmp_path / "2026-06-24.jsonl"
    rows = _read(path)
    assert len(rows) == 1
    r = rows[0]
    # the trailer fields the sink adds
    assert r["v"] == 1
    assert r["ts"] == "2026-06-24T12:00:00+00:00"
    assert r["source"] == "discretionary-cli"
    assert r["ticker"] == "NVDA"
    assert r["dry_run"] is False   # default
    assert r["run_id"] is None     # default
    # everything from compute() output, flattened in
    for k in ("track", "action", "cluster_pct", "effective_cap_pct",
              "regime_class", "breach", "ceiling_breach", "hard_ceiling_pct"):
        assert r[k] == _OUTPUT[k]


def test_append_decision_tags_dry_run_and_run_id(tmp_path):
    path = append_decision(
        output=_OUTPUT, fetched_at="t", ticker="NVDA", source="paper-auto-pipeline",
        dry_run=True, run_id="2026-06-24T05_11_40Z-abc", ledger_date=date(2026, 6, 24),
        calib_dir=tmp_path,
    )
    r = _read(path)[0]
    assert r["dry_run"] is True
    assert r["run_id"] == "2026-06-24T05_11_40Z-abc"


def test_append_is_append_not_overwrite(tmp_path):
    d = date(2026, 6, 24)
    append_decision(output=_OUTPUT, fetched_at="t1", ticker="NVDA",
                    source="discretionary-cli", ledger_date=d, calib_dir=tmp_path)
    append_decision(output={**_OUTPUT, "action": "block"}, fetched_at="t2",
                    ticker="MRVL", source="paper-auto-pipeline",
                    ledger_date=d, calib_dir=tmp_path)
    rows = _read(tmp_path / "2026-06-24.jsonl")
    assert len(rows) == 2
    assert rows[0]["ticker"] == "NVDA"
    assert rows[1]["ticker"] == "MRVL" and rows[1]["action"] == "block"


def test_append_creates_missing_dir(tmp_path):
    nested = tmp_path / "does" / "not" / "exist"
    path = append_decision(output=_OUTPUT, fetched_at="t", ticker="NVDA",
                           source="x", ledger_date=date(2026, 6, 24), calib_dir=nested)
    assert path.exists()
    assert len(_read(path)) == 1


def test_env_var_override(tmp_path, monkeypatch):
    monkeypatch.setenv(cluster_calibration.CALIB_DIR_ENV, str(tmp_path))
    path = append_decision(output=_OUTPUT, fetched_at="t", ticker="NVDA",
                           source="x", ledger_date=date(2026, 6, 24))
    assert path == tmp_path / "2026-06-24.jsonl"


def test_explicit_calib_dir_beats_env(tmp_path, monkeypatch):
    monkeypatch.setenv(cluster_calibration.CALIB_DIR_ENV, str(tmp_path / "from_env"))
    explicit = tmp_path / "from_arg"
    path = append_decision(output=_OUTPUT, fetched_at="t", ticker="NVDA",
                           source="x", ledger_date=date(2026, 6, 24), calib_dir=explicit)
    assert path.parent == explicit
