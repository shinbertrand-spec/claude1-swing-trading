"""Tests for tools.auto_paper.state — ledger I/O + positions.json append.

Uses tmp_path + monkeypatched module constants so tests don't write to the
real ledgers/paper-auto/ or journal/paper-auto/.
"""
from __future__ import annotations

import json
import os

import pytest
import yaml

from tools.auto_paper import state


@pytest.fixture
def paper_dirs(tmp_path, monkeypatch):
    """Redirect paper-auto paths into a tmp tree for a single test."""
    ledger_dir = tmp_path / "ledgers" / "paper-auto"
    positions_json = tmp_path / "journal" / "paper-auto" / "positions.json"
    monkeypatch.setattr(state, "PAPER_AUTO_LEDGER_DIR", str(ledger_dir))
    monkeypatch.setattr(state, "PAPER_AUTO_POSITIONS_JSON", str(positions_json))
    # Use the real project schema; tests below use minimal valid ledgers.
    return ledger_dir, positions_json


def test_ledger_path_uses_uppercase_ticker(paper_dirs):
    p = state.ledger_path("aapl")
    assert p.endswith("AAPL.yml")


def test_ledger_exists_negative(paper_dirs):
    assert state.ledger_exists("NVDA") is False


def test_load_ledger_missing(paper_dirs):
    with pytest.raises(state.PaperAutoStateError, match="no paper-auto ledger"):
        state.load_ledger("NVDA")


def test_write_submitted_ledger_happy(paper_dirs):
    path = state.write_submitted_ledger(
        ticker="NVDA",
        setup_type="EP",
        setup_grade="Swan",
        pivot_price=850.00,
        limit_price=850.50,
        stop_price=820.00,
        shares=10,
        broker_order_id=10001,
        broker="tiger_paper",
        sector_etf="XLK",
    )
    assert os.path.isfile(path)
    doc = yaml.safe_load(open(path))
    assert doc["meta"]["ticker"] == "NVDA"
    assert doc["meta"]["state"] == "submitted"
    assert doc["meta"]["account_track"] == "paper-auto"
    assert doc["setup_classification"]["type"] == "EP"
    assert doc["setup_classification"]["grade"] == "Swan"
    starter = doc["position_state"]["starter"]
    assert starter["shares"] == 10
    assert starter["fill_price"] == 850.50  # limit as placeholder until reconcile
    assert starter["initial_stop"] == 820.00
    assert starter["broker_order_id"] == 10001
    assert starter["broker"] == "tiger_paper"


def test_write_submitted_refuses_overwrite(paper_dirs):
    args = dict(
        ticker="NVDA", setup_type="EP", setup_grade=None,
        pivot_price=850.00, limit_price=850.50, stop_price=820.00,
        shares=10, broker_order_id=10001, broker="tiger_paper",
    )
    state.write_submitted_ledger(**args)
    with pytest.raises(state.PaperAutoStateError, match="already exists"):
        state.write_submitted_ledger(**args)


def test_write_submitted_overwrite_explicit(paper_dirs):
    args = dict(
        ticker="NVDA", setup_type="EP", setup_grade=None,
        pivot_price=850.00, limit_price=850.50, stop_price=820.00,
        shares=10, broker_order_id=10001, broker="tiger_paper",
    )
    state.write_submitted_ledger(**args)
    # overwrite=True must succeed
    state.write_submitted_ledger(**args, overwrite=True)


def test_write_submitted_passes_schema_validation(paper_dirs):
    """A ledger produced here must validate against ledgers/_schema/ledger.schema.json."""
    path = state.write_submitted_ledger(
        ticker="AAPL",
        setup_type="SEPA-VCP",
        setup_grade="A",
        pivot_price=180.00,
        limit_price=180.50,
        stop_price=174.00,
        shares=15,
        broker_order_id=10002,
        broker="tiger_paper",
        sector_etf="XLK",
    )
    # If validation failed inside write, it'd have raised. Sanity:
    assert os.path.isfile(path)


def test_append_positions_json_creates_file(paper_dirs):
    state.append_to_positions_json({
        "ticker": "NVDA", "ledger_path": "x.yml", "entry_date": "2026-05-24",
        "entry_price": 850.50, "shares": 10, "stop": 820.00,
        "target_1": 915.00, "sector": "XLK", "broker_order_id": 10001,
        "broker": "tiger_paper", "stage": "submitted",
        "setup_type": "EP", "setup_grade": "Swan",
    })
    data = json.load(open(state.PAPER_AUTO_POSITIONS_JSON))
    assert data["_account_track"] == "paper-auto"
    assert len(data["positions"]) == 1
    assert data["positions"][0]["ticker"] == "NVDA"


def test_append_positions_json_refuses_duplicate(paper_dirs):
    entry = {
        "ticker": "NVDA", "ledger_path": "x.yml", "entry_date": "2026-05-24",
        "entry_price": 850.50, "shares": 10, "stop": 820.00,
        "target_1": 915.00, "sector": "XLK", "broker_order_id": 10001,
        "broker": "tiger_paper", "stage": "submitted",
        "setup_type": "EP", "setup_grade": "Swan",
    }
    state.append_to_positions_json(entry)
    with pytest.raises(state.PaperAutoStateError, match="duplicate ticker"):
        state.append_to_positions_json(entry)


def test_append_positions_json_appends_to_existing(paper_dirs):
    state.append_to_positions_json({"ticker": "NVDA", "ledger_path": "x.yml"})
    state.append_to_positions_json({"ticker": "AAPL", "ledger_path": "y.yml"})
    data = json.load(open(state.PAPER_AUTO_POSITIONS_JSON))
    assert [p["ticker"] for p in data["positions"]] == ["NVDA", "AAPL"]


def test_load_positions_json_empty_missing(paper_dirs):
    """When the file doesn't exist, returns an empty positions index (no raise)."""
    data = state.load_positions_json()
    assert data == {"positions": []}
