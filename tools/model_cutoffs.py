"""Model knowledge-cutoff registry — evaluation-honesty policy (A1).

Per CLAUDE.md § Evaluation-honesty policies: any evaluation of *LLM-generated*
signal or judgment (critic-panel verdicts, debate outcomes, thematic picks,
news-agent calls) may be scored ONLY on market windows dated after the
underlying model's knowledge cutoff. Pre-cutoff windows may validate
deterministic rules only — the model may simply *remember* the period.

The conservative gate is the TRAINING DATA cutoff (the broader date range of
training data), not the "reliable knowledge" cutoff: memorization risk extends
to anything the model may have seen in training. Month-granularity dates from
the vendor docs are encoded as the LAST DAY of the stated month, so an eval
window is clean only if it starts strictly after that date.

Source: https://platform.claude.com/docs/en/about-claude/models/overview.md
(fetched 2026-07-24). Re-verify against that page when adding models — do not
guess cutoff dates.

CLI::

    uv run python -m tools.model_cutoffs claude-haiku-4-5-20251001
    uv run python -m tools.model_cutoffs claude-opus-4-8 --window-start 2025-06-01
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
from typing import Any, Optional

TOOL = "tools/model_cutoffs.py"

# prefix -> {training_data_cutoff, reliable_knowledge_cutoff} (ISO dates,
# month-end convention). Longest-prefix match resolves dated snapshot ids
# (e.g. "claude-haiku-4-5-20251001" -> "claude-haiku-4-5").
_REGISTRY: dict[str, dict[str, str]] = {
    "claude-fable-5":   {"training_data_cutoff": "2026-01-31", "reliable_knowledge_cutoff": "2026-01-31"},
    # Mythos 5 shares Fable 5's specs per the same docs page.
    "claude-mythos-5":  {"training_data_cutoff": "2026-01-31", "reliable_knowledge_cutoff": "2026-01-31"},
    "claude-opus-4-8":  {"training_data_cutoff": "2026-01-31", "reliable_knowledge_cutoff": "2026-01-31"},
    "claude-opus-4-7":  {"training_data_cutoff": "2026-01-31", "reliable_knowledge_cutoff": "2026-01-31"},
    "claude-opus-4-6":  {"training_data_cutoff": "2025-08-31", "reliable_knowledge_cutoff": "2025-05-31"},
    "claude-opus-4-5":  {"training_data_cutoff": "2025-08-31", "reliable_knowledge_cutoff": "2025-05-31"},
    "claude-opus-4-1":  {"training_data_cutoff": "2025-03-31", "reliable_knowledge_cutoff": "2025-01-31"},
    "claude-sonnet-5":  {"training_data_cutoff": "2026-01-31", "reliable_knowledge_cutoff": "2026-01-31"},
    "claude-sonnet-4-6": {"training_data_cutoff": "2026-01-31", "reliable_knowledge_cutoff": "2025-08-31"},
    "claude-sonnet-4-5": {"training_data_cutoff": "2025-07-31", "reliable_knowledge_cutoff": "2025-01-31"},
    "claude-haiku-4-5": {"training_data_cutoff": "2025-07-31", "reliable_knowledge_cutoff": "2025-02-28"},
}

# Longest prefix first so "claude-sonnet-4-6" wins over "claude-sonnet-4".
_PREFIXES = sorted(_REGISTRY, key=len, reverse=True)


def _resolve(model: str) -> Optional[dict[str, str]]:
    if not model:
        return None
    m = model.strip().lower()
    for prefix in _PREFIXES:
        if m == prefix or m.startswith(prefix + "-") or m.startswith(prefix + "@"):
            return _REGISTRY[prefix]
    return None


def cutoff_for(model: str) -> Optional[str]:
    """Return the TRAINING-DATA cutoff (ISO date) for ``model``, or None.

    This is the date eval artifacts should record as ``model_cutoff`` and the
    gate the post-cutoff evaluation policy is applied against. None means the
    model is not in the registry — record null and flag, never guess.
    """
    entry = _resolve(model)
    return entry["training_data_cutoff"] if entry else None


def reliable_cutoff_for(model: str) -> Optional[str]:
    """Return the vendor-stated 'reliable knowledge' cutoff, or None."""
    entry = _resolve(model)
    return entry["reliable_knowledge_cutoff"] if entry else None


def eval_window_contaminated(
    model: str, window_start: "str | _dt.date"
) -> Optional[bool]:
    """True if an eval window starting at ``window_start`` overlaps the
    model's possible memorization span (window_start <= training cutoff).

    Returns None when the model is unknown — the caller must surface that,
    not assume clean.
    """
    cutoff = cutoff_for(model)
    if cutoff is None:
        return None
    if isinstance(window_start, str):
        window_start = _dt.date.fromisoformat(window_start[:10])
    return window_start <= _dt.date.fromisoformat(cutoff)


def main() -> None:
    p = argparse.ArgumentParser(
        prog="tools.model_cutoffs",
        description="Look up a model's knowledge cutoff; optionally check an eval window.",
    )
    p.add_argument("model")
    p.add_argument(
        "--window-start", default=None,
        help="ISO date an eval window starts; adds a contamination verdict.",
    )
    args = p.parse_args()
    out: dict[str, Any] = {
        "model": args.model,
        "training_data_cutoff": cutoff_for(args.model),
        "reliable_knowledge_cutoff": reliable_cutoff_for(args.model),
    }
    if args.window_start:
        out["window_start"] = args.window_start
        out["contaminated"] = eval_window_contaminated(args.model, args.window_start)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
