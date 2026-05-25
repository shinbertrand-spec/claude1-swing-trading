"""Tests for tools.thematic_portfolio.corpus.manifest."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.thematic_portfolio.corpus.manifest import (
    CORPUS_SLOTS,
    SLOT_GLOBS,
    _snapshot_id,
    compose,
)


def _seed_corpus(root: Path, files: dict[str, list[str]]) -> dict[str, list[Path]]:
    """Build a corpus directory tree. files = {slot: [filename, ...]}."""
    created: dict[str, list[Path]] = {}
    for slot, names in files.items():
        if slot not in CORPUS_SLOTS:
            raise ValueError(f"unknown slot {slot}")
        slot_dir = root / CORPUS_SLOTS[slot]
        slot_dir.mkdir(parents=True, exist_ok=True)
        created[slot] = []
        for name in names:
            p = slot_dir / name
            if name.endswith(".json"):
                p.write_text(json.dumps({"id": name}), encoding="utf-8")
            else:
                p.write_text(f"# {name}\n", encoding="utf-8")
            created[slot].append(p)
    return created


def test_empty_corpus_root_returns_all_null_slots(tmp_path: Path):
    out = compose(corpus_root=tmp_path).output
    assert out["n_total_artifacts"] == 0
    for slot in CORPUS_SLOTS:
        assert out["paths"][slot] is None
        assert out["slot_counts"][slot] == 0


def test_corpus_root_missing_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="does not exist"):
        compose(corpus_root=tmp_path / "does-not-exist")


def test_corpus_root_is_file_raises(tmp_path: Path):
    f = tmp_path / "not-a-dir"
    f.write_text("x")
    with pytest.raises(ValueError, match="not a directory"):
        compose(corpus_root=f)


def test_populated_corpus_lists_per_slot_globs(tmp_path: Path):
    _seed_corpus(tmp_path, {
        "aschenbrenner_essays": ["chapter1.md", "chapter2.md"],
        "aschenbrenner_x": ["2026-05-20.json"],
        "shulman": ["80k-hours.md"],
    })
    out = compose(corpus_root=tmp_path).output
    assert out["paths"]["aschenbrenner_essays"] == "aschenbrenner/essays/*.md"
    assert out["paths"]["aschenbrenner_x"] == "aschenbrenner/x/*.json"
    assert out["paths"]["shulman"] == "shulman/*.md"
    # Unpopulated slots remain null.
    assert out["paths"]["trammell"] is None
    assert out["paths"]["press"] is None
    assert out["slot_counts"]["aschenbrenner_essays"] == 2
    assert out["slot_counts"]["aschenbrenner_x"] == 1
    assert out["n_total_artifacts"] == 4


def test_glob_matches_only_canonical_extension(tmp_path: Path):
    """essays glob is *.md — a stray .txt should NOT be counted."""
    _seed_corpus(tmp_path, {
        "aschenbrenner_essays": ["chapter1.md"],
    })
    # stray non-canonical file
    (tmp_path / CORPUS_SLOTS["aschenbrenner_essays"] / "notes.txt").write_text("stray")
    out = compose(corpus_root=tmp_path).output
    assert out["slot_counts"]["aschenbrenner_essays"] == 1


def test_since_filters_recent_artifacts(tmp_path: Path):
    _seed_corpus(tmp_path, {
        "aschenbrenner_essays": ["old.md", "new.md"],
    })
    essays_dir = tmp_path / CORPUS_SLOTS["aschenbrenner_essays"]

    # Set mtimes deterministically
    old_t = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
    new_t = (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()
    os.utime(essays_dir / "old.md", (old_t, old_t))
    os.utime(essays_dir / "new.md", (new_t, new_t))

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    out = compose(corpus_root=tmp_path, since=since).output
    assert out["recent_artifacts_since_last_loop1"] == ["aschenbrenner/essays/new.md"]
    assert out["n_recent_artifacts"] == 1


def test_since_none_returns_empty_recent_list(tmp_path: Path):
    """No prior Loop 1 firing → recent_artifacts is empty regardless of mtimes."""
    _seed_corpus(tmp_path, {
        "aschenbrenner_essays": ["new1.md", "new2.md"],
    })
    out = compose(corpus_root=tmp_path, since=None).output
    assert out["recent_artifacts_since_last_loop1"] == []
    assert out["n_recent_artifacts"] == 0


def test_since_accepts_iso_with_z_suffix(tmp_path: Path):
    """ISO-8601 with 'Z' suffix (common in JSON output) must parse cleanly."""
    _seed_corpus(tmp_path, {"aschenbrenner_essays": ["a.md"]})
    out = compose(corpus_root=tmp_path, since="2020-01-01T00:00:00Z").output
    # Files written just now > 2020 timestamp → all show up as recent
    assert out["n_recent_artifacts"] == 1


def test_since_accepts_iso_with_explicit_offset(tmp_path: Path):
    _seed_corpus(tmp_path, {"aschenbrenner_essays": ["a.md"]})
    out = compose(corpus_root=tmp_path, since="2020-01-01T00:00:00+00:00").output
    assert out["n_recent_artifacts"] == 1


def test_snapshot_id_is_stable_for_same_state(tmp_path: Path):
    """Identical refreshed_at + path list → identical snapshot_id."""
    paths = [tmp_path / "a", tmp_path / "b"]
    sid1 = _snapshot_id("2026-05-25T12:00:00Z", paths)
    sid2 = _snapshot_id("2026-05-25T12:00:00Z", paths)
    assert sid1 == sid2
    assert len(sid1) == 12


def test_snapshot_id_changes_when_artifact_added(tmp_path: Path):
    sid_before = _snapshot_id("T1", [tmp_path / "a"])
    sid_after = _snapshot_id("T1", [tmp_path / "a", tmp_path / "b"])
    assert sid_before != sid_after


def test_snapshot_id_order_independent(tmp_path: Path):
    """Sorting before hashing means order of input list doesn't matter."""
    sid1 = _snapshot_id("T", [tmp_path / "a", tmp_path / "b"])
    sid2 = _snapshot_id("T", [tmp_path / "b", tmp_path / "a"])
    assert sid1 == sid2


def test_relative_paths_use_forward_slashes(tmp_path: Path):
    """Recent-artifact paths must use forward slashes regardless of OS."""
    _seed_corpus(tmp_path, {
        "aschenbrenner_essays": ["a.md"],
    })
    new_t = datetime.now(timezone.utc).timestamp()
    os.utime(tmp_path / CORPUS_SLOTS["aschenbrenner_essays"] / "a.md", (new_t, new_t))
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    out = compose(corpus_root=tmp_path, since=since).output
    # Forward slashes always — important for Loop 1 prompt portability.
    assert "\\" not in out["recent_artifacts_since_last_loop1"][0]
    assert out["recent_artifacts_since_last_loop1"][0].startswith("aschenbrenner/essays/")


def test_output_shape_matches_loop1_input_contract(tmp_path: Path):
    """Regression check on the Loop 1 prompt's input contract."""
    out = compose(corpus_root=tmp_path).output
    # Per Loop 1 prompt § Input contract → corpus_snapshot keys
    for k in (
        "snapshot_id",
        "refreshed_at",
        "corpus_root",
        "paths",
        "slot_counts",
        "n_total_artifacts",
        "recent_artifacts_since_last_loop1",
        "n_recent_artifacts",
        "since",
    ):
        assert k in out, f"missing key {k!r} expected by Loop 1 contract"
    # paths dict must have all 7 Loop 1 slots
    for slot in (
        "aschenbrenner_essays",
        "aschenbrenner_x",
        "aschenbrenner_podcasts",
        "shulman",
        "trammell",
        "press",
        "secondary_sources",
    ):
        assert slot in out["paths"]


def test_trace_entry_inputs_round_trip(tmp_path: Path):
    """TraceEntry inputs must be JSON-serialisable per the Phase 4 contract."""
    entry = compose(corpus_root=tmp_path)
    json.dumps(entry.inputs)
    json.dumps(entry.output)


def test_slot_globs_cover_all_slots():
    """Every slot in CORPUS_SLOTS must have a matching glob — guards regressions."""
    assert set(SLOT_GLOBS.keys()) == set(CORPUS_SLOTS.keys())
