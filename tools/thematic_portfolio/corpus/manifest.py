"""Corpus snapshot composer — builds the input bundle the Loop 1 prompt consumes.

The :doc:`Loop 1 prompt </_draft/thematic-portfolio>` expects an input bundle
shaped like::

    corpus_snapshot:
      snapshot_id: <hash>
      refreshed_at: <ISO-8601>
      paths:
        aschenbrenner_essays: ledgers/thematic/corpus/aschenbrenner/essays/*.md
        aschenbrenner_x: ledgers/thematic/corpus/aschenbrenner/x/*.json
        ...
      recent_artifacts_since_last_loop1: [list of relative paths]

This module composes that snapshot by walking ``ledgers/thematic/corpus/``,
checking which subdirectories actually exist + have content, and (optionally)
computing the list of artifacts whose mtime exceeds a ``since`` timestamp
passed by the caller.

V1 explicitly **does not fetch corpus content** — that's the job of the
deferred X / podcast / press fetchers. The manifest composer just enumerates
what's already on disk and packages the paths into the snapshot. If a corpus
subdirectory is empty or missing, its glob slot is set to ``null`` in the
output.

CLI::

    uv run python -m tools.thematic_portfolio.corpus.manifest \\
        --corpus-root ledgers/thematic/corpus/ \\
        [--since 2026-05-01T00:00:00Z]
"""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from ...cli import emit
from ...contract import TraceEntry

TOOL = "tools/thematic_portfolio/corpus/manifest.py"

# Canonical corpus subdirectory layout. Loop 1 prompt's input contract
# names these slots; missing/empty subdirs surface as null in the snapshot.
CORPUS_SLOTS: dict[str, str] = {
    "aschenbrenner_essays": "aschenbrenner/essays",
    "aschenbrenner_x": "aschenbrenner/x",
    "aschenbrenner_podcasts": "aschenbrenner/podcasts",
    "shulman": "shulman",
    "trammell": "trammell",
    "press": "press",
    "secondary_sources": "secondary",
}

# File globs per slot. Single glob per slot is sufficient for v1; richer
# multi-glob slots can land in v2 if file conventions diversify.
SLOT_GLOBS: dict[str, str] = {
    "aschenbrenner_essays": "*.md",
    "aschenbrenner_x": "*.json",
    "aschenbrenner_podcasts": "*.md",
    "shulman": "*.md",
    "trammell": "*.md",
    "press": "*.md",
    "secondary_sources": "*.md",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating both 'Z' and explicit +00:00."""
    cleaned = s.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    return datetime.fromisoformat(cleaned)


def _list_artifacts(slot_dir: Path, glob: str) -> list[Path]:
    """List artifact files in a slot dir matching the slot's glob; deterministic order."""
    if not slot_dir.exists() or not slot_dir.is_dir():
        return []
    return sorted(slot_dir.glob(glob))


def _snapshot_id(refreshed_at: str, all_paths: list[Path]) -> str:
    """Hash refreshed_at + the sorted list of artifact paths into a stable ID.

    Used by Loop 1's drift detection to identify whether the corpus snapshot
    actually changed between firings. Hash is 12 hex chars of SHA-256 —
    short enough for a filename suffix, long enough that collisions are
    practically impossible at our cadence.
    """
    h = hashlib.sha256()
    h.update(refreshed_at.encode("utf-8"))
    for p in sorted(str(p) for p in all_paths):
        h.update(b"\0")
        h.update(p.encode("utf-8"))
    return h.hexdigest()[:12]


def compose(
    corpus_root: Path,
    since: str | None = None,
) -> TraceEntry:
    """Build the corpus_snapshot dict from the on-disk corpus tree.

    Args:
        corpus_root: directory containing the canonical subdirectory layout
            (see :data:`CORPUS_SLOTS`). Typically ``ledgers/thematic/corpus/``.
        since: optional ISO-8601 UTC timestamp. Files with mtime > since
            get listed in ``recent_artifacts_since_last_loop1``. If None,
            the recent-artifacts list is empty (no prior Loop 1 firing yet).

    Returns:
        TraceEntry whose output is the corpus_snapshot dict shaped per the
        Loop 1 prompt's input contract:

        * ``snapshot_id`` — 12-char hash of the refresh state
        * ``refreshed_at`` — ISO-8601 UTC
        * ``corpus_root`` — absolute path (string)
        * ``paths`` — per-slot glob string (relative to corpus_root) OR null
          when the slot is empty / missing
        * ``slot_counts`` — per-slot artifact count
        * ``recent_artifacts_since_last_loop1`` — sorted list of relative
          paths (relative to corpus_root) whose mtime > since
        * ``since`` — the input since timestamp, echoed for trace audit

    Raises:
        ValueError: corpus_root does not exist.
    """
    if not corpus_root.exists():
        raise ValueError(f"corpus_root does not exist: {corpus_root}")
    if not corpus_root.is_dir():
        raise ValueError(f"corpus_root is not a directory: {corpus_root}")

    refreshed_at = _utc_now_iso()
    since_dt = _parse_iso(since) if since else None

    paths_out: dict[str, str | None] = {}
    counts_out: dict[str, int] = {}
    all_paths: list[Path] = []
    recent_paths: list[Path] = []

    for slot, rel_subdir in CORPUS_SLOTS.items():
        slot_dir = corpus_root / rel_subdir
        glob = SLOT_GLOBS[slot]
        artifacts = _list_artifacts(slot_dir, glob)
        counts_out[slot] = len(artifacts)
        if artifacts:
            # Slot path expressed relative to corpus_root + the slot's glob,
            # so Loop 1 sees a glob expression it can expand against the root.
            paths_out[slot] = f"{rel_subdir}/{glob}"
            all_paths.extend(artifacts)
            if since_dt is not None:
                for p in artifacts:
                    try:
                        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                    except OSError:
                        continue
                    if mtime > since_dt:
                        recent_paths.append(p)
        else:
            paths_out[slot] = None

    snapshot_id = _snapshot_id(refreshed_at, all_paths)
    recent_rel = sorted(
        str(p.relative_to(corpus_root)).replace("\\", "/") for p in recent_paths
    )

    return TraceEntry(
        tool=TOOL,
        inputs={
            "corpus_root": str(corpus_root),
            "since": since,
        },
        output={
            "snapshot_id": snapshot_id,
            "refreshed_at": refreshed_at,
            "corpus_root": str(corpus_root),
            "paths": paths_out,
            "slot_counts": counts_out,
            "n_total_artifacts": len(all_paths),
            "recent_artifacts_since_last_loop1": recent_rel,
            "n_recent_artifacts": len(recent_rel),
            "since": since,
        },
    )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.thematic_portfolio.corpus.manifest",
        description=(
            "Build the corpus_snapshot the Loop 1 prompt consumes by walking "
            "ledgers/thematic/corpus/ and packaging the per-slot paths."
        ),
    )
    p.add_argument(
        "--corpus-root",
        type=Path,
        default=Path("ledgers/thematic/corpus"),
        help="Corpus root directory (default ledgers/thematic/corpus).",
    )
    p.add_argument(
        "--since",
        type=str,
        default=None,
        help=(
            "ISO-8601 UTC timestamp. Artifacts with mtime > since are listed in "
            "recent_artifacts_since_last_loop1. Pass the prior Loop 1 firing time."
        ),
    )
    args = p.parse_args()
    emit(compose(corpus_root=args.corpus_root, since=args.since))


if __name__ == "__main__":
    main()
