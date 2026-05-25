"""Tests for tools.broker.tiger.TigerClient with a mocked TradeClient.

Verifies:
  * paper-account routing (refuses live unless allow_live=True)
  * account_summary / positions / open_orders shape + masking
  * place_limit_buy / place_limit_sell — correct action, account, fields
  * cancel — returns broker-side global id
  * error paths wrap as BrokerOrderError

Never hits the live Tiger API.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.broker.tiger import (
    BrokerConfigError,
    BrokerOrderError,
    TigerClient,
)


# ---------------------------------------------------------------- fakes

class _FakeSummary(SimpleNamespace):
    """Stand-in for the SDK's Account summary object."""


class _FakePortfolioAccount(SimpleNamespace):
    """PortfolioAccount with .summary."""


class _FakeContract(SimpleNamespace):
    pass


class _FakePosition(SimpleNamespace):
    pass


class _FakeOrder(SimpleNamespace):
    pass


class FakeTradeClient:
    """Drop-in for tigeropen TradeClient. Records calls + returns canned data."""

    def __init__(self, *, account_full="PAPER87654321", is_paper=True):
        self.account_full = account_full
        self.config_info = {
            "tiger_id_masked": "...5678",
            "account_masked": "...4321",
            "license": "TBSG",
            "is_paper": is_paper,
            "server_url": "https://mock",
            "props_dir": "/mock",
        }
        self.calls: list[tuple] = []
        self._next_order_id = 10_000

        # Canned data
        self.assets_to_return = [
            _FakePortfolioAccount(summary=_FakeSummary(
                cash=950_000.0,
                available_funds=940_000.0,
                buying_power=1_880_000.0,
                net_liquidation=1_000_000.0,
                gross_position_value=50_000.0,
                currency="USD",
            ))
        ]
        self.positions_to_return = [
            _FakePosition(
                contract=_FakeContract(symbol="AAPL"),
                quantity=100,
                average_cost=180.50,
                market_value=18_500.0,
                unrealized_pnl=450.0,
            ),
        ]
        self.contracts_by_symbol: dict[str, _FakeContract] = {}
        self.open_orders_to_return: list[_FakeOrder] = []
        self.cancel_response: int | None = 9999

    # --- mirror of SDK surface --------------------------------------------

    def get_assets(self, *, account, segment=False, **_):
        self.calls.append(("get_assets", account, segment))
        return self.assets_to_return

    def get_positions(self, *, account, **_):
        self.calls.append(("get_positions", account))
        return self.positions_to_return

    def get_open_orders(self, *, account, **_):
        self.calls.append(("get_open_orders", account))
        return self.open_orders_to_return

    def get_contract(self, *, symbol, **_):
        self.calls.append(("get_contract", symbol))
        return self.contracts_by_symbol.get(
            symbol, _FakeContract(symbol=symbol, sec_type="STK", currency="USD"),
        )

    def place_order(self, order):
        self.calls.append((
            "place_order",
            order.account, order.action, order.quantity,
            order.limit_price, order.contract.symbol,
        ))
        order_id = self._next_order_id
        self._next_order_id += 1
        order.id = order_id
        return order_id

    def cancel_order(self, *, account, id, **_):
        self.calls.append(("cancel_order", account, id))
        return self.cancel_response

    def get_filled_orders(self, *, account, start_time=None, end_time=None,
                          symbol=None, **_):
        self.calls.append(("get_filled_orders", account, start_time, end_time, symbol))
        return getattr(self, "filled_orders_to_return", [])


class _FakeBrief(SimpleNamespace):
    pass


class FakeQuoteClient:
    """Drop-in for tigeropen QuoteClient.get_briefs."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.briefs_by_symbol: dict[str, _FakeBrief] = {}
        self.raise_on_call = False

    def get_briefs(self, *, symbols, include_ask_bid=False, **_):
        self.calls.append(("get_briefs", tuple(symbols), include_ask_bid))
        if self.raise_on_call:
            raise RuntimeError("QUOTE_API_ERROR")
        return [
            self.briefs_by_symbol[s] for s in symbols
            if s in self.briefs_by_symbol
        ]


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def paper_client() -> TigerClient:
    fake = FakeTradeClient(is_paper=True)
    return TigerClient(_trade_client=fake, _quote_client=FakeQuoteClient())


# ---------------------------------------------------------------- tests


def test_construct_paper_via_test_injection():
    fake = FakeTradeClient(is_paper=True)
    c = TigerClient(_trade_client=fake)
    info = c.config_info
    assert info["is_paper"] is True
    assert info["account_masked"] == "...4321"


def test_construct_refuses_live_by_default(monkeypatch, tmp_path):
    """The real construct-path: load_config returns is_paper=False → refuse."""
    (tmp_path / "tiger_openapi_config.properties").write_text("# stub\n")

    class _LiveCfg:
        tiger_id = "DEV111"
        account = "LIVE9999"
        license = "TBSG"
        is_paper = False
        server_url = "https://mock"

    monkeypatch.setattr(
        "tigeropen.tiger_open_config.TigerOpenClientConfig",
        lambda **kw: _LiveCfg(),
    )
    with pytest.raises(BrokerConfigError, match="not a paper account"):
        TigerClient(props_dir=str(tmp_path))


def test_construct_live_with_allow_live(monkeypatch, tmp_path):
    (tmp_path / "tiger_openapi_config.properties").write_text("# stub\n")

    class _LiveCfg:
        tiger_id = "DEV111"
        account = "LIVE9999"
        license = "TBSG"
        is_paper = False
        server_url = "https://mock"

    monkeypatch.setattr(
        "tigeropen.tiger_open_config.TigerOpenClientConfig",
        lambda **kw: _LiveCfg(),
    )

    # Stub the TradeClient + QuoteClient classes so construct doesn't actually
    # try to call live.
    class _StubTC:
        def __init__(self, cfg):
            self.cfg = cfg

    class _StubQC:
        def __init__(self, cfg, logger=None):
            self.cfg = cfg

    monkeypatch.setattr(
        "tigeropen.trade.trade_client.TradeClient",
        _StubTC,
    )
    monkeypatch.setattr(
        "tigeropen.quote.quote_client.QuoteClient",
        _StubQC,
    )
    c = TigerClient(props_dir=str(tmp_path), allow_live=True)
    assert c.config_info["is_paper"] is False


def test_account_summary_shape(paper_client):
    entry = paper_client.account_summary()
    assert entry.tool == "tools/broker/tiger.py"
    assert entry.inputs == {"call": "account_summary"}
    out = entry.output
    assert out["account_masked"] == "...4321"
    assert out["is_paper"] is True
    assert out["cash"] == 950_000.0
    assert out["available_funds"] == 940_000.0
    assert out["buying_power"] == 1_880_000.0
    assert out["net_liquidation"] == 1_000_000.0
    assert out["gross_position_value"] == 50_000.0
    assert out["currency"] == "USD"


def test_account_summary_wraps_sdk_error(paper_client):
    def _boom(*a, **kw):
        raise RuntimeError("HTTP 503")

    paper_client._tc.get_assets = _boom
    with pytest.raises(BrokerOrderError, match="get_assets failed: HTTP 503"):
        paper_client.account_summary()


def test_positions_shape(paper_client):
    entry = paper_client.positions()
    out = entry.output
    assert out["n_positions"] == 1
    p = out["positions"][0]
    assert p["symbol"] == "AAPL"
    assert p["quantity"] == 100
    assert p["average_cost"] == 180.50
    assert p["market_value"] == 18_500.0
    assert p["unrealized_pnl"] == 450.0


def test_positions_empty(paper_client):
    paper_client._tc.positions_to_return = []
    entry = paper_client.positions()
    assert entry.output["n_positions"] == 0
    assert entry.output["positions"] == []


def test_place_limit_buy_records_correct_fields(paper_client):
    entry = paper_client.place_limit_buy("NVDA", quantity=10, limit_price=850.50)
    out = entry.output
    assert out["order_id"] == 10_000
    assert out["symbol"] == "NVDA"
    assert out["action"] == "BUY"
    assert out["quantity"] == 10
    assert out["limit_price"] == 850.50
    assert out["is_paper"] is True

    # The SDK got the right call.
    fake = paper_client._tc
    place_call = [c for c in fake.calls if c[0] == "place_order"][0]
    _, account, action, qty, limit_price, symbol = place_call
    assert account == "PAPER87654321"
    assert action == "BUY"
    assert qty == 10
    assert limit_price == 850.50
    assert symbol == "NVDA"

    # And inputs carry masked PII only.
    assert entry.inputs["account_masked"] == "...4321"
    assert "PAPER87654321" not in str(entry.inputs)


def test_place_limit_sell_records_action(paper_client):
    entry = paper_client.place_limit_sell("AAPL", quantity=50, limit_price=190.00)
    assert entry.output["action"] == "SELL"
    place_call = [c for c in paper_client._tc.calls if c[0] == "place_order"][0]
    assert place_call[2] == "SELL"


def test_place_limit_validates_quantity(paper_client):
    with pytest.raises(BrokerOrderError, match="quantity must be positive"):
        paper_client.place_limit_buy("AAPL", quantity=0, limit_price=100)


def test_place_limit_validates_price(paper_client):
    with pytest.raises(BrokerOrderError, match="limit_price must be positive"):
        paper_client.place_limit_buy("AAPL", quantity=10, limit_price=-1)


def test_place_limit_handles_unknown_contract(paper_client):
    paper_client._tc.get_contract = lambda *, symbol, **_: None
    with pytest.raises(BrokerOrderError, match="no contract found"):
        paper_client.place_limit_buy("ZZZZ", quantity=10, limit_price=10)


def test_place_limit_wraps_place_order_error(paper_client):
    def _boom(order):
        raise RuntimeError("INSUFFICIENT_FUNDS")

    paper_client._tc.place_order = _boom
    with pytest.raises(BrokerOrderError, match="INSUFFICIENT_FUNDS"):
        paper_client.place_limit_buy("AAPL", quantity=10, limit_price=100)


def test_cancel_returns_global_id(paper_client):
    paper_client._tc.cancel_response = 55_555
    entry = paper_client.cancel(order_id=10_000)
    assert entry.output == {
        "order_id": 10_000,
        "cancelled_id": 55_555,
        "accepted": True,
    }


def test_cancel_no_response(paper_client):
    paper_client._tc.cancel_response = None
    entry = paper_client.cancel(order_id=10_000)
    assert entry.output["accepted"] is False
    assert entry.output["cancelled_id"] is None


def test_cancel_wraps_error(paper_client):
    def _boom(*a, **kw):
        raise RuntimeError("ORDER_NOT_FOUND")

    paper_client._tc.cancel_order = _boom
    with pytest.raises(BrokerOrderError, match="ORDER_NOT_FOUND"):
        paper_client.cancel(order_id=10_000)


def test_place_stop_loss_basic(paper_client):
    entry = paper_client.place_stop_loss("NVDA", quantity=10, stop_price=820.00)
    out = entry.output
    assert out["order_id"] == 10_000
    assert out["symbol"] == "NVDA"
    assert out["action"] == "SELL"
    assert out["order_type"] == "STP"
    assert out["quantity"] == 10
    assert out["stop_price"] == 820.00
    assert out["is_paper"] is True

    place_call = [c for c in paper_client._tc.calls if c[0] == "place_order"][0]
    _, account, action, qty, _limit, symbol = place_call
    assert account == "PAPER87654321"
    assert action == "SELL"
    assert qty == 10
    assert symbol == "NVDA"

    assert entry.inputs["account_masked"] == "...4321"
    assert "PAPER87654321" not in str(entry.inputs)


def test_place_stop_loss_validates_quantity(paper_client):
    with pytest.raises(BrokerOrderError, match="quantity must be positive"):
        paper_client.place_stop_loss("NVDA", quantity=0, stop_price=820)


def test_place_stop_loss_validates_stop_price(paper_client):
    with pytest.raises(BrokerOrderError, match="stop_price must be positive"):
        paper_client.place_stop_loss("NVDA", quantity=10, stop_price=-1)


def test_place_stop_loss_no_contract(paper_client):
    paper_client._tc.get_contract = lambda *, symbol, **_: None
    with pytest.raises(BrokerOrderError, match="no contract found"):
        paper_client.place_stop_loss("ZZZZ", quantity=10, stop_price=10)


def test_place_stop_loss_wraps_place_error(paper_client):
    def _boom(order):
        raise RuntimeError("MARKET_CLOSED")

    paper_client._tc.place_order = _boom
    with pytest.raises(BrokerOrderError, match="MARKET_CLOSED"):
        paper_client.place_stop_loss("NVDA", quantity=10, stop_price=820)


def test_get_filled_orders_empty(paper_client):
    paper_client._tc.filled_orders_to_return = []
    entry = paper_client.get_filled_orders()
    assert entry.output["n_orders"] == 0
    assert entry.output["orders"] == []
    assert entry.inputs["call"] == "get_filled_orders"


def test_get_filled_orders_shape(paper_client):
    paper_client._tc.filled_orders_to_return = [
        _FakeOrder(
            id=99_001,
            contract=_FakeContract(symbol="NVDA"),
            action="BUY",
            order_type="LMT",
            quantity=10,
            filled=10,
            avg_fill_price=849.75,
            limit_price=850.00,
            status="Filled",
            trade_time="2026-05-24T13:45:12Z",
        ),
    ]
    entry = paper_client.get_filled_orders(start_time="2026-05-24")
    out = entry.output
    assert out["n_orders"] == 1
    o = out["orders"][0]
    assert o["order_id"] == 99_001
    assert o["symbol"] == "NVDA"
    assert o["filled_quantity"] == 10
    assert o["avg_fill_price"] == 849.75
    assert o["limit_price"] == 850.00


def test_get_filled_orders_wraps_error(paper_client):
    def _boom(*a, **kw):
        raise RuntimeError("RATE_LIMIT")

    paper_client._tc.get_filled_orders = _boom
    with pytest.raises(BrokerOrderError, match="RATE_LIMIT"):
        paper_client.get_filled_orders()


def test_open_orders_shape(paper_client):
    paper_client._tc.open_orders_to_return = [
        _FakeOrder(
            id=12345,
            contract=_FakeContract(symbol="NVDA"),
            action="BUY",
            order_type="LMT",
            quantity=10,
            limit_price=850.0,
            status="Submitted",
        ),
    ]
    entry = paper_client.open_orders()
    out = entry.output
    assert out["n_orders"] == 1
    o = out["orders"][0]
    assert o["order_id"] == 12345
    assert o["symbol"] == "NVDA"
    assert o["action"] == "BUY"
    assert o["order_type"] == "LMT"
    assert o["quantity"] == 10
    assert o["limit_price"] == 850.0
    assert o["status"] == "Submitted"


# ---------------------------------------------------------------- get_quote


def test_get_quote_returns_bid_ask_last(paper_client):
    paper_client._qc.briefs_by_symbol["NVDA"] = _FakeBrief(
        symbol="NVDA", bid_price=850.10, ask_price=850.50,
        latest_price=850.30, bid_size=100, ask_size=200,
        halted=False, delay=0,
    )
    entry = paper_client.get_quote("NVDA")
    out = entry.output
    assert out["symbol"] == "NVDA"
    assert out["bid_price"] == 850.10
    assert out["ask_price"] == 850.50
    assert out["latest_price"] == 850.30
    assert out["bid_size"] == 100
    assert out["ask_size"] == 200
    assert out["halted"] is False
    assert entry.inputs["call"] == "get_quote"
    assert entry.inputs["account_masked"] == "...4321"


def test_get_quote_passes_include_ask_bid_true(paper_client):
    paper_client._qc.briefs_by_symbol["AAPL"] = _FakeBrief(
        symbol="AAPL", bid_price=190.0, ask_price=190.1,
        latest_price=190.05, bid_size=10, ask_size=20,
        halted=False, delay=0,
    )
    paper_client.get_quote("AAPL")
    call = paper_client._qc.calls[0]
    assert call[0] == "get_briefs"
    assert call[1] == ("AAPL",)
    assert call[2] is True  # include_ask_bid


def test_get_quote_raises_on_empty_briefs(paper_client):
    # symbol not in briefs_by_symbol -> empty list -> error
    with pytest.raises(BrokerOrderError, match="No quote returned"):
        paper_client.get_quote("ZZZZ")


def test_get_quote_wraps_sdk_error(paper_client):
    paper_client._qc.raise_on_call = True
    with pytest.raises(BrokerOrderError, match="QUOTE_API_ERROR"):
        paper_client.get_quote("NVDA")


def test_get_quote_raises_when_quote_client_none():
    fake = FakeTradeClient(is_paper=True)
    client = TigerClient(_trade_client=fake, _quote_client=None)
    with pytest.raises(BrokerOrderError, match="QuoteClient not initialised"):
        client.get_quote("NVDA")
