"""Tests for tools.auto_paper.pipeline — placement orchestration with mocked broker."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.auto_paper import state
from tools.auto_paper.pipeline import (
    CandidateInput,
    _check_track_limits,
    place_candidate,
)


# ------------------------------------------------------- fakes


class FakeTradeClient:
    """Minimal stand-in matching TigerClient's surface (injection seam)."""
    def __init__(self, *, net_liq=1_000_000.0, cash=950_000.0, is_paper=True):
        self.account_full = "PAPER87654321"
        self.config_info = {
            "tiger_id_masked": "...5678",
            "account_masked": "...4321",
            "license": "TBSG",
            "is_paper": is_paper,
            "server_url": "https://mock",
            "props_dir": "/mock",
        }
        self.summary_assets = [
            SimpleNamespace(summary=SimpleNamespace(
                cash=cash,
                available_funds=cash,
                buying_power=cash * 2,
                net_liquidation=net_liq,
                gross_position_value=net_liq - cash,
                currency="USD",
            ))
        ]
        self.next_order_id = 10_000
        self.calls: list[tuple] = []

    def get_assets(self, *, account, segment=False, **_):
        return self.summary_assets

    def get_positions(self, *, account, **_):
        return []

    def get_open_orders(self, *, account, **_):
        return []

    def get_contract(self, *, symbol, **_):
        return SimpleNamespace(symbol=symbol, sec_type="STK", currency="USD")

    def place_order(self, order):
        order_id = self.next_order_id
        self.next_order_id += 1
        order.id = order_id
        self.calls.append((
            "place_order", order.account, order.action, order.quantity,
            order.limit_price, order.contract.symbol,
        ))
        return order_id


@pytest.fixture
def paper_dirs(tmp_path, monkeypatch):
    ledger_dir = tmp_path / "ledgers" / "paper-auto"
    positions_json = tmp_path / "journal" / "paper-auto" / "positions.json"
    monkeypatch.setattr(state, "PAPER_AUTO_LEDGER_DIR", str(ledger_dir))
    monkeypatch.setattr(state, "PAPER_AUTO_POSITIONS_JSON", str(positions_json))
    return ledger_dir, positions_json


@pytest.fixture
def paper_client(paper_dirs):
    from tools.broker.tiger import TigerClient
    return TigerClient(_trade_client=FakeTradeClient())


def _vcp_cand(**over):
    base = dict(
        ticker="NVDA", setup_type="SEPA-VCP", setup_grade="A",
        pivot_price=850.00, limit_price=850.50, stop_price=820.00,
        target_price=910.00, shares=10, sector_etf="XLK",
    )
    base.update(over)
    return CandidateInput(**base)


# ------------------------------------------------------- limit-check unit


def test_track_limits_clean():
    assert _check_track_limits(
        cand=_vcp_cand(),
        account_net_liq=1_000_000.0,
        existing_positions=[],
        existing_cash=950_000.0,
    ) is None


def test_track_limits_position_count():
    fake_positions = [{"ticker": f"T{i}", "sector": "XLU"} for i in range(8)]
    reason = _check_track_limits(
        cand=_vcp_cand(),
        account_net_liq=1_000_000.0,
        existing_positions=fake_positions,
        existing_cash=950_000.0,
    )
    assert "position count limit" in reason


def test_track_limits_per_position():
    # 10000 shares × $850 = $8.5M > 5% of $1M
    reason = _check_track_limits(
        cand=_vcp_cand(shares=10_000),
        account_net_liq=1_000_000.0,
        existing_positions=[],
        existing_cash=950_000.0,
    )
    assert "5% cap" in reason


def test_track_limits_sector_cap():
    # Engineer: per-position passes (4.25%) but combined sector trips 20%.
    # NVDA add = 50 × $850.50 = $42,525 = 4.25% (under 5%)
    # Existing XLK: 1600 MSFT × $105 = $168,000 = 16.8%
    # Combined sector = $210,525 = 21.05% > 20%
    existing = [{"ticker": "MSFT", "shares": 1600, "entry_price": 105.00, "sector": "XLK"}]
    reason = _check_track_limits(
        cand=_vcp_cand(shares=50),
        account_net_liq=1_000_000.0,
        existing_positions=existing,
        existing_cash=950_000.0,
    )
    assert "XLK" in reason and "20% cap" in reason


def test_track_limits_cash_buffer():
    # Engineer: per-position passes (4.25%) but cash drops under 15%.
    # cost = 50 × $850.50 = $42,525
    # existing_cash = $180,000 (= 18% of $1M); after = $137,475 = 13.75% < 15%
    reason = _check_track_limits(
        cand=_vcp_cand(shares=50),
        account_net_liq=1_000_000.0,
        existing_positions=[],
        existing_cash=180_000.0,
    )
    assert "15% cash buffer" in reason


def test_track_limits_zero_net_liq():
    reason = _check_track_limits(
        cand=_vcp_cand(),
        account_net_liq=0.0,
        existing_positions=[],
        existing_cash=0.0,
    )
    assert "net_liquidation" in reason


# ------------------------------------------------------- place_candidate


def test_rejects_non_deployable(paper_client):
    cand = _vcp_cand(setup_type="Pullback-20SMA")
    result = place_candidate(cand, client=paper_client, dry_run=True)
    assert result.status == "rejected"
    assert "not on deployable list" in result.reason


def test_rejects_existing_ledger(paper_client, paper_dirs):
    state.write_submitted_ledger(
        ticker="NVDA", setup_type="EP", setup_grade=None,
        pivot_price=850, limit_price=850.5, stop_price=820,
        shares=10, broker_order_id=1, broker="tiger_paper",
    )
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=True)
    assert result.status == "rejected"
    assert "already exists" in result.reason


def test_dry_run_does_not_place_or_write(paper_client, paper_dirs):
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=True)
    assert result.status == "dry_run"
    assert "would place limit-buy" in result.reason
    assert result.cost_estimate_usd == 10 * 850.50
    # No file should have been written
    assert not state.ledger_exists("NVDA")
    assert paper_client._tc.calls == []


def test_real_run_places_and_writes(paper_client, paper_dirs):
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=False)
    assert result.status == "placed"
    assert result.broker_order_id == 10_000
    assert result.ledger_path is not None
    assert state.ledger_exists("NVDA")
    # Positions.json append
    pj = state.load_positions_json()
    assert len(pj["positions"]) == 1
    assert pj["positions"][0]["ticker"] == "NVDA"
    assert pj["positions"][0]["broker_order_id"] == 10_000
    assert pj["positions"][0]["broker"] == "tiger_paper"
    # Broker actually got called
    place_calls = [c for c in paper_client._tc.calls if c[0] == "place_order"]
    assert len(place_calls) == 1


def test_rejects_when_count_limit_hit(paper_client, paper_dirs):
    # Pre-load 8 positions so count limit binds
    for i in range(8):
        state.append_to_positions_json({"ticker": f"T{i}", "sector": "XLU"})
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=False)
    assert result.status == "rejected"
    assert "position count" in result.reason


def test_returns_error_on_broker_summary_failure(paper_client, paper_dirs):
    def _boom(*a, **kw):
        raise RuntimeError("HTTP 503")
    paper_client._tc.get_assets = _boom
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=False)
    assert result.status == "error"
    assert "account_summary" in result.reason


def test_returns_error_on_broker_place_failure(paper_client, paper_dirs):
    def _boom(order):
        raise RuntimeError("INSUFFICIENT_FUNDS")
    paper_client._tc.place_order = _boom
    result = place_candidate(_vcp_cand(), client=paper_client, dry_run=False)
    assert result.status == "error"
    assert "INSUFFICIENT_FUNDS" in result.reason
