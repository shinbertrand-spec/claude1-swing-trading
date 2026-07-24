"""Tests for the 2026-07-24 close-writer fix: structured exit fields on
realized closes, the `unfilled` flag on expiry, and symmetric clearing on the
flip-back path (tools.auto_paper.reconcile)."""
from __future__ import annotations

import yaml
import pytest

from tools.auto_paper import reconcile as rc
from tools.auto_paper import state


def _seed_ledger(tmp_path, monkeypatch, ticker="TST", meta_state="starter"):
    monkeypatch.setattr(state, "PAPER_AUTO_LEDGER_DIR", str(tmp_path))
    doc = {
        "meta": {
            "schema_version": "1.0", "ticker": ticker,
            "asof": "2026-07-01T14:00:00+00:00", "state": meta_state,
            "account_track": "paper-auto",
            "ledger_path": f"ledgers/paper-auto/{ticker}.yml",
            "created_by": "auto_paper/pipeline",
            "created_at": "2026-07-01T14:00:00+00:00",
            "updated_by": "auto_paper/pipeline",
            "updated_at": "2026-07-01T14:00:00+00:00",
        },
        "setup_classification": {
            "type": "ts_momentum_liquid_us", "pivot_price": 100.0, "stop_price": 92.0,
            "stop_distance_pct": 0.08, "trace_refs": [],
            "confluence_checklist": [], "grade": "B",
        },
        "position_state": {
            "stage": "STARTER", "intended_full_shares": 50,
            "starter": {
                "trigger": "QuantSignal", "fill_date": "2026-07-01",
                "shares": 50, "fill_price": 100.5,
                "limit_price_placed": 100.5, "initial_stop": 92.0,
                "trace_refs": [], "broker_order_id": 111, "broker": "tiger_paper",
            },
            "current_stop": 92.0,
        },
        "reasoning_trace": [],
        "regime": {"sector_etf": "XLK", "computed_at": "2026-07-01T14:00:00+00:00"},
    }
    with open(state.ledger_path(ticker), "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)
    return ticker


def _load(ticker):
    with open(state.ledger_path(ticker), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(autouse=True)
def _no_calibration(monkeypatch):
    # _apply_realized_close feeds calibration best-effort; tests must not
    # write real calibration rows.
    monkeypatch.setattr(
        rc.critic_panel, "record_calibration_outcome", lambda *a, **k: None,
    )


class TestRealizedClose:
    def test_writes_structured_exit_fields(self, tmp_path, monkeypatch):
        t = _seed_ledger(tmp_path, monkeypatch)
        rc._apply_realized_close(t, exit_price=104.25, exit_reason="protective stop filled")
        doc = _load(t)
        ps = doc["position_state"]
        assert doc["meta"]["state"] == "closed"
        assert ps["exit_price"] == 104.25
        assert ps["exit_reason"] == "protective stop filled"
        assert ps["exit_date"]  # today's ISO date, non-empty
        assert "Closed by auto_paper/reconcile" in doc["notes"]

    def test_written_doc_validates_against_schema(self, tmp_path, monkeypatch):
        t = _seed_ledger(tmp_path, monkeypatch)
        rc._apply_realized_close(t, exit_price=104.25, exit_reason="x")
        rc._validate_against_schema(_load(t))  # must not raise


class TestExpiredUnfilled:
    def test_sets_unfilled_flag(self, tmp_path, monkeypatch):
        t = _seed_ledger(tmp_path, monkeypatch, meta_state="submitted")
        rc._update_ledger_expired(t, "TIF=DAY expired unfilled")
        doc = _load(t)
        assert doc["meta"]["state"] == "closed"
        assert doc["position_state"]["unfilled"] is True
        assert "expired unfilled" in doc["notes"]
        rc._validate_against_schema(doc)


class TestFlipBackClears:
    def test_flip_to_starter_clears_exit_fields_and_unfilled(self, tmp_path, monkeypatch):
        t = _seed_ledger(tmp_path, monkeypatch, meta_state="closed")
        # simulate a bogus close that must be undone
        doc = _load(t)
        doc["position_state"].update(
            exit_price=104.0, exit_date="2026-07-02", exit_reason="bogus",
            unfilled=True,
        )
        with open(state.ledger_path(t), "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, sort_keys=False)
        rc._flip_to_starter_from_closed(t, reason="broker still holds")
        ps = _load(t)["position_state"]
        for field in ("exit_price", "exit_date", "exit_reason", "unfilled"):
            assert field not in ps
