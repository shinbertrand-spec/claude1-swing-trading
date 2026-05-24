"""Tiger Brokers Open Platform — paper-trading bridge.

Per ``project_broker_bridge.md`` memory + CLAUDE.md § Sensitive Information:

* The credentials directory lives OUTSIDE this repo at
  ``C:/Users/User/Desktop/tiger/`` (default). The directory holds a
  ``tiger_openapi_config.properties`` file (Tiger's standard format —
  tiger_id, account, private_key_pk8, license, env) which the SDK reads.
* Account numbers are PII — :func:`load_config` returns them MASKED
  (last-4 only). The full account number is needed for SDK calls but is
  never logged or returned in the public dict.
* The private key value NEVER leaves the SDK layer — :class:`TigerClient`
  does not expose it on its public surface.
* :class:`TigerClient` refuses to construct against a live account unless
  ``allow_live=True`` is passed explicitly. Paper-only by default.

Every public method returns a :class:`TraceEntry` so the broker call can
be appended to a position ledger's ``reasoning_trace`` — same audit
contract as the Phase 2 arithmetic tools.

Public surface:

    load_config(props_dir=None) -> dict
    class TigerClient:
        account_summary()                              -> TraceEntry
        positions()                                    -> TraceEntry
        place_limit_buy(symbol, quantity, limit_price) -> TraceEntry
        place_limit_sell(symbol, quantity, limit_price)-> TraceEntry
        cancel(order_id)                               -> TraceEntry
        open_orders()                                  -> TraceEntry
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from ..contract import TraceEntry

TOOL = "tools/broker/tiger.py"

CREDENTIALS_DIR_DEFAULT = "C:/Users/User/Desktop/tiger"
PROPS_FILENAME = "tiger_openapi_config.properties"


class BrokerConfigError(RuntimeError):
    """Raised when the Tiger credentials cannot be loaded or are invalid."""


class BrokerOrderError(RuntimeError):
    """Raised when Tiger rejects an order or order-lifecycle call."""


def _mask(value: Optional[str]) -> str:
    """Return ``...XXXX`` (last 4 chars) — for any PII in user-facing output."""
    if value is None:
        return "****"
    s = str(value)
    if len(s) < 4:
        return "****"
    return f"...{s[-4:]}"


def load_config(props_dir: Optional[str] = None) -> dict[str, Any]:
    """Load Tiger credentials and return a SAFE-TO-LOG dict.

    The returned dict masks tiger_id + account to last-4. The full values
    are kept inside the wrapped :class:`TigerOpenClientConfig` (used by
    :class:`TigerClient`) but never exposed by this function.

    Args:
        props_dir: directory containing ``tiger_openapi_config.properties``.
            Defaults to ``$TIGER_PROPS_DIR`` env var or
            :data:`CREDENTIALS_DIR_DEFAULT`.

    Raises:
        BrokerConfigError: if the directory or properties file is missing,
            if required fields are absent, or if the SDK fails to parse.
    """
    if props_dir is None:
        props_dir = os.environ.get("TIGER_PROPS_DIR", CREDENTIALS_DIR_DEFAULT)

    if not os.path.isdir(props_dir):
        raise BrokerConfigError(
            f"Tiger credentials directory not found: {props_dir}. "
            f"Set TIGER_PROPS_DIR or place the props file at the default location."
        )
    props_file = os.path.join(props_dir, PROPS_FILENAME)
    if not os.path.isfile(props_file):
        raise BrokerConfigError(
            f"Missing {PROPS_FILENAME} in {props_dir}. "
            f"Download from the Tiger Developer Info dashboard."
        )

    try:
        from tigeropen.tiger_open_config import TigerOpenClientConfig
    except ImportError as exc:
        raise BrokerConfigError(
            "tigeropen SDK not installed. Run `uv sync` after uncommenting "
            "tigeropen in pyproject.toml."
        ) from exc

    try:
        cfg = TigerOpenClientConfig(props_path=props_dir)
    except Exception as exc:
        raise BrokerConfigError(f"Tiger SDK failed to parse config: {exc}") from exc

    if not cfg.tiger_id or not cfg.account:
        raise BrokerConfigError(
            "Tiger config missing tiger_id or account. Check the properties file."
        )

    return {
        "tiger_id_masked": _mask(cfg.tiger_id),
        "account_masked": _mask(cfg.account),
        "license": cfg.license,
        "is_paper": bool(cfg.is_paper),
        "server_url": cfg.server_url,
        "props_dir": props_dir,
    }


@dataclass
class _OrderResult:
    order_id: int
    symbol: str
    action: str
    quantity: float
    limit_price: float


class TigerClient:
    """Paper-routed Tiger Brokers client.

    Refuses to construct against a live account unless ``allow_live=True``.

    Args:
        props_dir: directory containing ``tiger_openapi_config.properties``.
            Defaults to ``$TIGER_PROPS_DIR`` env var or
            :data:`CREDENTIALS_DIR_DEFAULT`.
        allow_live: when False (default), raises :class:`BrokerConfigError`
            if the loaded account is not a paper account.

    Raises:
        BrokerConfigError: on config-load failure or live-account refusal.
    """

    def __init__(
        self,
        props_dir: Optional[str] = None,
        allow_live: bool = False,
        _trade_client: Any = None,  # injected for tests
    ) -> None:
        if props_dir is None:
            props_dir = os.environ.get("TIGER_PROPS_DIR", CREDENTIALS_DIR_DEFAULT)

        if _trade_client is not None:
            # Test path: caller supplied a pre-built (mocked) trade client +
            # a config-like object via the .config attribute on it.
            self._tc = _trade_client
            self._config_info = getattr(_trade_client, "config_info", {
                "tiger_id_masked": "****",
                "account_masked": "****",
                "license": None,
                "is_paper": True,
                "server_url": "https://mock",
                "props_dir": props_dir,
            })
            self._account = getattr(_trade_client, "account_full", "PAPER-MOCK")
            return

        self._config_info = load_config(props_dir)
        if not self._config_info["is_paper"] and not allow_live:
            raise BrokerConfigError(
                f"Loaded account {self._config_info['account_masked']} is not a "
                f"paper account. Pass allow_live=True to construct against live."
            )

        try:
            from tigeropen.tiger_open_config import TigerOpenClientConfig
            from tigeropen.trade.trade_client import TradeClient
        except ImportError as exc:
            raise BrokerConfigError(
                "tigeropen SDK not installed; cannot build TigerClient."
            ) from exc

        cfg = TigerOpenClientConfig(props_path=props_dir)
        self._account = cfg.account
        self._tc = TradeClient(cfg)

    @property
    def config_info(self) -> dict[str, Any]:
        """Return the safe-to-log config dict (masked PII)."""
        return dict(self._config_info)

    # ------------------------------------------------------------------ reads

    def account_summary(self) -> TraceEntry:
        """Return cash + buying power + net liquidation for the routed account."""
        try:
            assets = self._tc.get_assets(account=self._account, segment=False)
        except Exception as exc:
            raise BrokerOrderError(f"get_assets failed: {exc}") from exc

        asset = assets[0] if isinstance(assets, list) and assets else assets
        summary = asset.summary if hasattr(asset, "summary") else asset

        out = {
            "account_masked": self._config_info["account_masked"],
            "is_paper": self._config_info["is_paper"],
            "cash": float(getattr(summary, "cash", 0.0) or 0.0),
            "available_funds": float(getattr(summary, "available_funds", 0.0) or 0.0),
            "buying_power": float(getattr(summary, "buying_power", 0.0) or 0.0),
            "net_liquidation": float(getattr(summary, "net_liquidation", 0.0) or 0.0),
            "gross_position_value": float(getattr(summary, "gross_position_value", 0.0) or 0.0),
            "currency": getattr(summary, "currency", "USD"),
        }
        return TraceEntry(
            tool=TOOL,
            inputs={"call": "account_summary"},
            output=out,
        )

    def positions(self) -> TraceEntry:
        """Return open positions for the routed account."""
        try:
            raw = self._tc.get_positions(account=self._account)
        except Exception as exc:
            raise BrokerOrderError(f"get_positions failed: {exc}") from exc

        positions = []
        for p in raw or []:
            positions.append({
                "symbol": getattr(p.contract, "symbol", None) if hasattr(p, "contract") else None,
                "quantity": float(getattr(p, "quantity", 0) or 0),
                "average_cost": float(getattr(p, "average_cost", 0.0) or 0.0),
                "market_value": float(getattr(p, "market_value", 0.0) or 0.0),
                "unrealized_pnl": float(getattr(p, "unrealized_pnl", 0.0) or 0.0),
            })
        return TraceEntry(
            tool=TOOL,
            inputs={"call": "positions"},
            output={
                "account_masked": self._config_info["account_masked"],
                "n_positions": len(positions),
                "positions": positions,
            },
        )

    def open_orders(self) -> TraceEntry:
        """Return open (unfilled) orders for the routed account."""
        try:
            raw = self._tc.get_open_orders(account=self._account)
        except Exception as exc:
            raise BrokerOrderError(f"get_open_orders failed: {exc}") from exc

        orders = []
        for o in raw or []:
            orders.append({
                "order_id": getattr(o, "id", None) or getattr(o, "order_id", None),
                "symbol": getattr(o.contract, "symbol", None) if hasattr(o, "contract") else None,
                "action": getattr(o, "action", None),
                "order_type": getattr(o, "order_type", None),
                "quantity": float(getattr(o, "quantity", 0) or 0),
                "limit_price": (
                    float(getattr(o, "limit_price", 0.0)) if getattr(o, "limit_price", None) is not None else None
                ),
                "status": getattr(o, "status", None),
            })
        return TraceEntry(
            tool=TOOL,
            inputs={"call": "open_orders"},
            output={
                "account_masked": self._config_info["account_masked"],
                "n_orders": len(orders),
                "orders": orders,
            },
        )

    # ----------------------------------------------------------------- writes

    def place_limit_buy(
        self, symbol: str, quantity: float, limit_price: float,
    ) -> TraceEntry:
        """Place a paper limit-buy. Returns a TraceEntry with the broker order id."""
        return self._place_limit(symbol, "BUY", quantity, limit_price)

    def place_limit_sell(
        self, symbol: str, quantity: float, limit_price: float,
    ) -> TraceEntry:
        """Place a paper limit-sell. Returns a TraceEntry with the broker order id."""
        return self._place_limit(symbol, "SELL", quantity, limit_price)

    def _place_limit(
        self, symbol: str, action: str, quantity: float, limit_price: float,
    ) -> TraceEntry:
        if quantity <= 0:
            raise BrokerOrderError(f"quantity must be positive; got {quantity}")
        if limit_price <= 0:
            raise BrokerOrderError(f"limit_price must be positive; got {limit_price}")
        if action not in ("BUY", "SELL"):
            raise BrokerOrderError(f"action must be BUY or SELL; got {action}")

        try:
            from tigeropen.common.util.order_utils import limit_order
        except ImportError as exc:
            raise BrokerOrderError("tigeropen SDK not installed") from exc

        try:
            contract = self._tc.get_contract(symbol=symbol)
        except Exception as exc:
            raise BrokerOrderError(f"get_contract({symbol}) failed: {exc}") from exc
        if contract is None:
            raise BrokerOrderError(f"no contract found for symbol {symbol}")

        order = limit_order(
            account=self._account,
            contract=contract,
            action=action,
            quantity=quantity,
            limit_price=limit_price,
        )
        try:
            order_id = self._tc.place_order(order)
        except Exception as exc:
            raise BrokerOrderError(
                f"place_order({action} {quantity} {symbol} @ {limit_price}) failed: {exc}"
            ) from exc

        return TraceEntry(
            tool=TOOL,
            inputs={
                "call": "place_limit",
                "symbol": symbol,
                "action": action,
                "quantity": quantity,
                "limit_price": limit_price,
                "account_masked": self._config_info["account_masked"],
            },
            output={
                "order_id": int(order_id) if order_id is not None else None,
                "symbol": symbol,
                "action": action,
                "quantity": quantity,
                "limit_price": limit_price,
                "is_paper": self._config_info["is_paper"],
            },
        )

    def place_stop_loss(
        self, symbol: str, quantity: float, stop_price: float,
    ) -> TraceEntry:
        """Place a paper stop-loss SELL order (closes a long if price drops to stop).

        The order is a STP (not STP-LMT) — fires a market order when stop_price
        is touched. This is the right primitive for protective stops on long
        positions; for slippage-control on fast gaps use a stop-limit variant
        (deferred — Session 3).
        """
        if quantity <= 0:
            raise BrokerOrderError(f"quantity must be positive; got {quantity}")
        if stop_price <= 0:
            raise BrokerOrderError(f"stop_price must be positive; got {stop_price}")

        try:
            from tigeropen.common.util.order_utils import stop_order
        except ImportError as exc:
            raise BrokerOrderError("tigeropen SDK not installed") from exc

        try:
            contract = self._tc.get_contract(symbol=symbol)
        except Exception as exc:
            raise BrokerOrderError(f"get_contract({symbol}) failed: {exc}") from exc
        if contract is None:
            raise BrokerOrderError(f"no contract found for symbol {symbol}")

        order = stop_order(
            account=self._account,
            contract=contract,
            action="SELL",
            quantity=quantity,
            aux_price=stop_price,
        )
        try:
            order_id = self._tc.place_order(order)
        except Exception as exc:
            raise BrokerOrderError(
                f"place_stop_loss({quantity} {symbol} stop={stop_price}) failed: {exc}"
            ) from exc

        return TraceEntry(
            tool=TOOL,
            inputs={
                "call": "place_stop_loss",
                "symbol": symbol,
                "quantity": quantity,
                "stop_price": stop_price,
                "account_masked": self._config_info["account_masked"],
            },
            output={
                "order_id": int(order_id) if order_id is not None else None,
                "symbol": symbol,
                "action": "SELL",
                "order_type": "STP",
                "quantity": quantity,
                "stop_price": stop_price,
                "is_paper": self._config_info["is_paper"],
            },
        )

    def get_filled_orders(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> TraceEntry:
        """Return filled orders for the routed account within a time window.

        Args:
            start_time: ISO datetime or YYYY-MM-DD; SDK accepts both.
                When None, defaults to today's session at the broker.
            end_time: ISO datetime or YYYY-MM-DD; None = now.
            symbol: optional symbol filter.

        Used by EOD reconciliation to pick up actual fill prices for orders
        placed via place_limit_buy / place_limit_sell.
        """
        try:
            raw = self._tc.get_filled_orders(
                account=self._account,
                start_time=start_time,
                end_time=end_time,
                symbol=symbol,
            )
        except Exception as exc:
            raise BrokerOrderError(f"get_filled_orders failed: {exc}") from exc

        orders = []
        for o in raw or []:
            orders.append({
                "order_id": getattr(o, "id", None) or getattr(o, "order_id", None),
                "symbol": getattr(o.contract, "symbol", None) if hasattr(o, "contract") else None,
                "action": getattr(o, "action", None),
                "order_type": getattr(o, "order_type", None),
                "quantity": float(getattr(o, "quantity", 0) or 0),
                "filled_quantity": float(getattr(o, "filled", 0) or 0),
                "avg_fill_price": (
                    float(getattr(o, "avg_fill_price", 0.0))
                    if getattr(o, "avg_fill_price", None) is not None else None
                ),
                "limit_price": (
                    float(getattr(o, "limit_price", 0.0))
                    if getattr(o, "limit_price", None) is not None else None
                ),
                "status": getattr(o, "status", None),
                "trade_time": getattr(o, "trade_time", None),
            })
        return TraceEntry(
            tool=TOOL,
            inputs={
                "call": "get_filled_orders",
                "start_time": start_time,
                "end_time": end_time,
                "symbol": symbol,
            },
            output={
                "account_masked": self._config_info["account_masked"],
                "n_orders": len(orders),
                "orders": orders,
            },
        )

    def cancel(self, order_id: int) -> TraceEntry:
        """Cancel an open order by broker (global) order id."""
        try:
            returned_id = self._tc.cancel_order(account=self._account, id=order_id)
        except Exception as exc:
            raise BrokerOrderError(f"cancel_order({order_id}) failed: {exc}") from exc

        return TraceEntry(
            tool=TOOL,
            inputs={
                "call": "cancel",
                "order_id": order_id,
                "account_masked": self._config_info["account_masked"],
            },
            output={
                "order_id": order_id,
                "cancelled_id": int(returned_id) if returned_id is not None else None,
                "accepted": returned_id is not None,
            },
        )
