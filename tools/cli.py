"""Common CLI shim used by ``python -m tools.<name>`` invocations."""
from __future__ import annotations

from .contract import TraceEntry


def emit(entry: TraceEntry) -> None:
    """Print the :class:`TraceEntry` to stdout as indented JSON."""
    print(entry.to_json())
