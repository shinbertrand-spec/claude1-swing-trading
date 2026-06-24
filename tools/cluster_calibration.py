"""Append-only calibration sink for the cross-track cluster-cap decisions.

Pure instrumentation — records EVERY ``cluster_concentration`` decision (allow /
warn / half_size / block) regardless of placement outcome, on BOTH tracks, so we
can later measure whether the regime-conditional cap (6e992a7) actually bit.

Distinct from the 6e992a7 ``reasoning_trace`` append, which only persists for
PLACED candidates (the ``_reject`` path discards ``cand.reasoning_trace``, and
only the placed path writes a ledger) — i.e. it records the automated track's
``allow`` near-misses but DROPS its ``block`` cases, the exact "did the
regime-conditioning bite" events. This sink captures the blocks, on both tracks.

Cross-track location (NOT under ``paper-auto/``) because the cap is cross-track::

    ledgers/cluster-cap/_calibration/YYYY-MM-DD.jsonl   (one JSON per line)

Leaf module: imports only stdlib — nothing from ``pipeline`` /
``cluster_concentration`` — so there is no import cycle. Mirrors the convention of
:func:`tools.auto_paper.critic_panel.append_calibration_log` (date-partitioned
JSONL, append mode, ``mkdir``, an injectable directory for tests).
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]
CALIBRATION_DIR = _ROOT / "ledgers" / "cluster-cap" / "_calibration"

# Test / override hook — matches the panel's ``panel_dir`` param convention but
# also honours an env var so callers that can't pass a param (the pipeline money
# path, the CLI) can still redirect writes in tests. Precedence: explicit
# ``calib_dir`` arg > env var > the default cross-track dir.
CALIB_DIR_ENV = "CLUSTER_CALIB_DIR"


def _resolve_dir(calib_dir: Optional[Path]) -> Path:
    if calib_dir is not None:
        return Path(calib_dir)
    env = os.environ.get(CALIB_DIR_ENV)
    if env:
        return Path(env)
    return CALIBRATION_DIR


def append_decision(
    *,
    output: dict[str, Any],
    fetched_at: str,
    ticker: str,
    source: str,
    ledger_date: date | None = None,
    calib_dir: Path | None = None,
) -> Path:
    """Append one cluster-cap decision to
    ``ledgers/cluster-cap/_calibration/YYYY-MM-DD.jsonl`` (one JSON per line).

    Args:
        output: the ``cluster_concentration.compute()`` output dict — already
            carries ``track`` / ``action`` / ``breach`` / ``ceiling_breach`` /
            ``cluster_pct`` / ``effective_cap_pct`` / ``hard_ceiling_pct`` /
            ``regime_class`` / ``theme`` / ``*_usd``.
        fetched_at: the decision's ISO timestamp (the ``TraceEntry.fetched_at``).
        ticker: the candidate ticker.
        source: which write site produced this — ``paper-auto-pipeline`` or
            ``discretionary-cli``.
        ledger_date: partition date (defaults to today).
        calib_dir: override the output directory (tests); see ``CALIB_DIR_ENV``.

    Returns the path written. Append-only — never rewrites an existing line.
    """
    if ledger_date is None:
        ledger_date = date.today()
    cal_dir = _resolve_dir(calib_dir)
    cal_dir.mkdir(parents=True, exist_ok=True)
    path = cal_dir / f"{ledger_date.isoformat()}.jsonl"
    record = {"ts": fetched_at, "source": source, "ticker": ticker, **output}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return path
