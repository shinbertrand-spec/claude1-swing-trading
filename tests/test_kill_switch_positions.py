"""Tests for tools.thematic_portfolio.kill_switch.positions — thematic-index
intersect with Tiger positions."""
from __future__ import annotations

import json
from pathlib import Path

from tools.thematic_portfolio.kill_switch.positions import (
    identify_thematic_positions,
    load_thematic_index,
)


def _write_index(path: Path, tickers: list[str]) -> None:
    doc = {
        "schema_version": "1.0",
        "updated": "2026-05-25T20:00:00+00:00",
        "positions": [
            {
                "ticker": t,
                "shares": 100,
                "cost_basis": 100.0,
                "ledger_path": f"ledgers/thematic-portfolio/{t}.yml",
                "loop1_firing_id": "loop1-test",
                "added_at": "2026-05-25T20:00:00+00:00",
            }
            for t in tickers
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _tiger_pos(symbol, qty=100, market_value=10_000.0, average_cost=95.0):
    return {
        "symbol": symbol,
        "quantity": qty,
        "average_cost": average_cost,
        "market_value": market_value,
        "unrealized_pnl": market_value - qty * average_cost,
    }


# --- index file missing ---------------------------------------------------


def test_index_missing_returns_empty_thematic_book(tmp_path):
    index_path = tmp_path / "positions.json"
    tiger = [_tiger_pos("NVDA"), _tiger_pos("AAPL")]

    result = identify_thematic_positions(tiger, index_path=index_path)
    assert result.index_missing is True
    assert result.thematic_positions == []
    assert result.thematic_market_value == 0.0
    assert result.thematic_symbols == []
    assert result.tiger_only_symbols == ["AAPL", "NVDA"]
    assert result.index_only_symbols == []
    assert any("missing" in w for w in result.warnings)


def test_index_empty_returns_empty_thematic_book(tmp_path):
    index_path = tmp_path / "positions.json"
    _write_index(index_path, [])
    tiger = [_tiger_pos("NVDA")]

    result = identify_thematic_positions(tiger, index_path=index_path)
    assert result.index_missing is False
    assert result.thematic_positions == []
    assert result.tiger_only_symbols == ["NVDA"]


# --- intersection ---------------------------------------------------------


def test_intersection_returns_only_indexed_positions(tmp_path):
    index_path = tmp_path / "positions.json"
    _write_index(index_path, ["NVDA", "BE", "CRWV"])
    tiger = [
        _tiger_pos("NVDA", qty=100, market_value=15_000.0),
        _tiger_pos("AAPL", qty=50, market_value=8_000.0),   # human-track
        _tiger_pos("BE",   qty=200, market_value=8_000.0),
    ]

    result = identify_thematic_positions(tiger, index_path=index_path)
    assert result.thematic_symbols == ["BE", "NVDA"]
    assert result.tiger_only_symbols == ["AAPL"]
    assert result.index_only_symbols == ["CRWV"]  # in index, not held
    assert result.thematic_market_value == 23_000.0


def test_case_insensitive_symbol_matching(tmp_path):
    index_path = tmp_path / "positions.json"
    _write_index(index_path, ["nvda"])  # lowercase in index
    tiger = [_tiger_pos("NVDA")]
    result = identify_thematic_positions(tiger, index_path=index_path)
    assert result.thematic_symbols == ["NVDA"]


def test_index_metadata_preserved_on_match(tmp_path):
    index_path = tmp_path / "positions.json"
    _write_index(index_path, ["NVDA"])
    tiger = [_tiger_pos("NVDA")]
    result = identify_thematic_positions(tiger, index_path=index_path)
    assert result.thematic_positions[0].index_metadata["ledger_path"].endswith("NVDA.yml")


def test_drift_warning_when_index_lists_unheld_positions(tmp_path):
    index_path = tmp_path / "positions.json"
    _write_index(index_path, ["NVDA", "BE", "CRWV"])
    tiger = [_tiger_pos("NVDA")]
    result = identify_thematic_positions(tiger, index_path=index_path)
    assert result.index_only_symbols == ["BE", "CRWV"]
    assert any("not held at Tiger" in w for w in result.warnings)


def test_load_thematic_index_handles_missing_ticker_field(tmp_path):
    index_path = tmp_path / "positions.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps({
        "positions": [
            {"shares": 100},  # malformed — no ticker
            {"ticker": "NVDA", "shares": 100},
        ]
    }), encoding="utf-8")

    index_map, missing = load_thematic_index(index_path)
    assert missing is False
    assert list(index_map.keys()) == ["NVDA"]
