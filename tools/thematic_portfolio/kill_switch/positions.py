"""Thematic-position identifier — intersect Tiger positions with the
thematic-portfolio index.

Per [[swing-thematic-portfolio-kill-switch-architecture]] § "Communication
between processes":

    One-way only: B reads from A's position-delta queue to know what's
    "thematic" vs. swing-book. (Otherwise B might unwind a swing-book
    position by mistake.)

The "position-delta queue" in our implementation is
``journal/thematic-portfolio/positions.json`` (parallel to the existing
``journal/positions.json`` for the human-discretionary track and
``journal/paper-auto/positions.json`` for the autonomous track).

Until the thematic-portfolio actually places trades — paper-only until
Q3 2026 calibration cycle — this file may be missing or contain an empty
positions array. The identifier must handle that gracefully (return an
empty thematic subset, kill-switch cycle becomes a no-op heartbeat).

## Index file schema

::

    {
      "schema_version": "1.0",
      "updated": "<iso-8601>",
      "positions": [
        {
          "ticker": "NVDA",
          "shares": 100,
          "cost_basis": 145.20,
          "ledger_path": "ledgers/thematic-portfolio/NVDA.yml",
          "loop1_firing_id": "loop1-2026-06-01T12:00:00Z",
          "added_at": "2026-06-01T13:15:22Z"
        },
        ...
      ]
    }

Only ``ticker`` is required; the rest is informational. The identifier
treats the file as a SYMBOL ALLOWLIST: a Tiger position is "thematic"
iff its symbol appears in this index.

## Why an allowlist (and not a tag at the broker)

Tiger Open API does not support broker-side position tagging. We could
embed the tag in the per-ledger YAML (and we do — ``meta.account_track``
is part of the schema), but Process B running 60s polling shouldn't have
to read all per-ticker YAML files every cycle. A single positions.json
index is cheap to read and easy to keep in sync.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_THEMATIC_INDEX_PATH = Path("journal/thematic-portfolio/positions.json")


@dataclass
class ThematicPosition:
    """One Tiger position confirmed to be in the thematic-portfolio book."""

    ticker: str
    shares: float
    market_value: float
    average_cost: float
    unrealized_pnl: float
    index_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ThematicReadResult:
    """Output of :func:`identify_thematic_positions`."""

    thematic_positions: list[ThematicPosition]
    thematic_market_value: float
    thematic_symbols: list[str]
    tiger_only_symbols: list[str]  # Tiger holds these but not in thematic index
    index_only_symbols: list[str]  # thematic index lists these but not in Tiger
    index_missing: bool
    index_path: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "thematic_positions": [p.to_dict() for p in self.thematic_positions],
            "thematic_market_value": self.thematic_market_value,
            "thematic_symbols": self.thematic_symbols,
            "tiger_only_symbols": self.tiger_only_symbols,
            "index_only_symbols": self.index_only_symbols,
            "index_missing": self.index_missing,
            "index_path": self.index_path,
            "warnings": self.warnings,
        }


def load_thematic_index(
    index_path: Optional[Path] = None,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Load the thematic-portfolio positions index.

    Returns ``(symbol_to_metadata, index_missing)``. If the file does
    not exist, returns ``({}, True)`` — the caller decides whether that
    is an error or just an empty book.
    """
    path = index_path or DEFAULT_THEMATIC_INDEX_PATH
    if not path.exists():
        return {}, True
    doc = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for entry in doc.get("positions", []):
        ticker = entry.get("ticker")
        if not ticker:
            continue
        out[ticker.upper()] = dict(entry)
    return out, False


def identify_thematic_positions(
    tiger_positions: list[dict[str, Any]],
    index_path: Optional[Path] = None,
) -> ThematicReadResult:
    """Intersect Tiger positions with the thematic index.

    Args:
        tiger_positions: list of dicts as produced by
            ``TigerClient.positions().output["positions"]`` — each has
            ``symbol``, ``quantity``, ``average_cost``, ``market_value``,
            ``unrealized_pnl``.
        index_path: override the default thematic index location (used
            in tests).

    Returns a :class:`ThematicReadResult` with the intersection +
    out-of-band symbols on each side (for drift reporting).
    """
    index_map, missing = load_thematic_index(index_path)
    warnings: list[str] = []
    if missing:
        warnings.append(
            f"Thematic index missing at {index_path or DEFAULT_THEMATIC_INDEX_PATH} — "
            "treating as empty thematic book."
        )

    tiger_symbols = {
        (p.get("symbol") or "").upper(): p
        for p in tiger_positions
        if p.get("symbol")
    }

    thematic_positions: list[ThematicPosition] = []
    for sym, meta in index_map.items():
        tiger_pos = tiger_symbols.get(sym)
        if tiger_pos is None:
            continue
        thematic_positions.append(
            ThematicPosition(
                ticker=sym,
                shares=float(tiger_pos.get("quantity", 0) or 0),
                market_value=float(tiger_pos.get("market_value", 0.0) or 0.0),
                average_cost=float(tiger_pos.get("average_cost", 0.0) or 0.0),
                unrealized_pnl=float(tiger_pos.get("unrealized_pnl", 0.0) or 0.0),
                index_metadata=meta,
            )
        )

    thematic_symbols = sorted(p.ticker for p in thematic_positions)
    tiger_only = sorted(sym for sym in tiger_symbols if sym not in index_map)
    index_only = sorted(sym for sym in index_map if sym not in tiger_symbols)

    if index_only:
        warnings.append(
            f"Thematic index lists {len(index_only)} symbol(s) not held at "
            f"Tiger: {','.join(index_only)}. Possible reconciliation drift "
            "(may be partially-filled orders or stale index entries)."
        )

    return ThematicReadResult(
        thematic_positions=thematic_positions,
        thematic_market_value=sum(p.market_value for p in thematic_positions),
        thematic_symbols=thematic_symbols,
        tiger_only_symbols=tiger_only,
        index_only_symbols=index_only,
        index_missing=missing,
        index_path=str(index_path or DEFAULT_THEMATIC_INDEX_PATH),
        warnings=warnings,
    )
