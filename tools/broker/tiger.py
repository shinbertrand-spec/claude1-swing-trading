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
        _quote_client: Any = None,  # injected for tests
    ) -> None:
        if props_dir is None:
            props_dir = os.environ.get("TIGER_PROPS_DIR", CREDENTIALS_DIR_DEFAULT)

        if _trade_client is not None:
            # Test path: caller supplied a pre-built (mocked) trade client +
            # a config-like object via the .config attribute on it.
            self._tc = _trade_client
            self._qc = _quote_client
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
            from tigeropen.quote.quote_client import QuoteClient
        except ImportError as exc:
            raise BrokerConfigError(
                "tigeropen SDK not installed; cannot build TigerClient."
            ) from exc

        cfg = TigerOpenClientConfig(props_path=props_dir)
        self._account = cfg.account
        self._tc = TradeClient(cfg)
        self._qc = QuoteClient(cfg)

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

        # Absence-of-evidence guard (broadened): None or any non-list is an
        # unconfirmed soft-failure, not a flat account. Raise so the sweeps fail safe.
        if not isinstance(raw, (list, tuple)):
            raise BrokerOrderError(
                f"get_positions returned {type(raw).__name__} (unconfirmed) — "
                f"refusing to treat as flat"
            )
        positions = []
        for p in raw:
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

        # Absence-of-evidence guard (2026-06-20, broadened): a successful empty
        # book is an empty LIST; None — or any non-list — is an unconfirmed
        # soft-failure. Treating it as "no orders" would let reconcile abandon a
        # genuinely-live order. Raise so the caller fails safe.
        if not isinstance(raw, (list, tuple)):
            raise BrokerOrderError(
                f"get_open_orders returned {type(raw).__name__} (unconfirmed) — "
                f"refusing to treat as empty"
            )
        orders = []
        for o in raw:
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
                "user_mark": getattr(o, "user_mark", None),
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

    def get_quote(self, symbol: str) -> TraceEntry:
        """Return current bid/ask/last for a US equity symbol.

        Uses tigeropen's ``QuoteClient.get_briefs(include_ask_bid=True)``
        which returns a list of ``QuoteBrief`` objects with bid_price,
        ask_price, latest_price, bid_size, ask_size, halted, delay fields.

        Used by the thematic-portfolio kill-switch (Process B) to compute
        a limit-sell price = bid * 0.999 on emergency exits. Tight quote
        coupling is the safer architectural choice than yfinance: the
        broker we're selling INTO is the broker we should be reading
        quotes from.

        Raises:
            BrokerOrderError: when the quote call fails or no brief is
                returned for the symbol (halted / delisted / wrong sec_type).
        """
        if self._qc is None:
            raise BrokerOrderError(
                "QuoteClient not initialised; cannot fetch quote."
            )
        try:
            briefs = self._qc.get_briefs(
                symbols=[symbol], include_ask_bid=True,
            )
        except Exception as exc:
            raise BrokerOrderError(f"get_briefs({symbol}) failed: {exc}") from exc

        if not briefs:
            raise BrokerOrderError(
                f"No quote returned for {symbol} (halted / delisted / wrong sec_type?)"
            )

        b = briefs[0]
        out = {
            "symbol": getattr(b, "symbol", symbol),
            "bid_price": float(getattr(b, "bid_price", 0.0) or 0.0),
            "ask_price": float(getattr(b, "ask_price", 0.0) or 0.0),
            "latest_price": float(getattr(b, "latest_price", 0.0) or 0.0),
            "bid_size": int(getattr(b, "bid_size", 0) or 0),
            "ask_size": int(getattr(b, "ask_size", 0) or 0),
            "halted": bool(getattr(b, "halted", False) or False),
            "delay": int(getattr(b, "delay", 0) or 0),
        }
        return TraceEntry(
            tool=TOOL,
            inputs={
                "call": "get_quote",
                "symbol": symbol,
                "account_masked": self._config_info["account_masked"],
            },
            output=out,
        )

    # ----------------------------------------------------------------- writes

    def place_limit_buy(
        self, symbol: str, quantity: float, limit_price: float,
        *, user_mark: str | None = None,
    ) -> TraceEntry:
        """Place a paper limit-buy. Returns a TraceEntry with the broker order id.

        ``user_mark`` stamps a deterministic client-order tag on the broker order
        (the auto-paper write-ahead cloid) so a crash-orphaned order can be
        recovered by EXACT TAG ECHO rather than a symbol/qty/limit heuristic
        (origin proof — never adopts a human order). Best-effort: if the SDK /
        paper API does not persist the tag, recovery falls back to the strict
        heuristic match.
        """
        return self._place_limit(symbol, "BUY", quantity, limit_price, user_mark=user_mark)

    def place_limit_sell(
        self, symbol: str, quantity: float, limit_price: float,
        *, user_mark: str | None = None,
    ) -> TraceEntry:
        """Place a paper limit-sell. Returns a TraceEntry with the broker order id."""
        return self._place_limit(symbol, "SELL", quantity, limit_price, user_mark=user_mark)

    def _place_limit(
        self, symbol: str, action: str, quantity: float, limit_price: float,
        *, user_mark: str | None = None,
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
        # Stamp the client-order tag (origin proof for crash recovery). Best-
        # effort: the Order dataclass exposes user_mark; if a future SDK drops it
        # this must not break placement.
        if user_mark is not None:
            try:
                order.user_mark = str(user_mark)[:32]
            except Exception:  # noqa: BLE001
                pass
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

    def place_auction_limit_buy(
        self, symbol: str, quantity: float, limit_price: float,
        *, user_mark: str | None = None,
    ) -> TraceEntry:
        """Place a paper LIMIT-ON-OPEN (auction limit, order type AL) BUY.

        Participates in the opening auction: fills at the opening print when
        open <= limit, else does not execute. This is the same-session-entry
        doctrine's LOO mechanism (2026-08-06 review, GO-NARROW) — a LIMIT
        order, so the CLAUDE.md "never place a market order" hard rule holds.

        CAPABILITY STATUS: DENIED on Tiger paper for US equities (2026-08-06,
        both probes). Pre-market placement AND during-RTH placement each
        rejected with ApiException 1200 "Only limit orders ..." — the venue
        does not accept AL in either window the doctrine could use, so this
        method is expected to raise BrokerOrderError if ever called. Kept as
        capability documentation with its tests; NO production call sites.
        Doctrine fallback: plain wide-limit DAY order placed at 09:30:00
        (spec only). Per the challenge work-through, ts_momentum should NOT
        switch — the +3% chase cap measured as adverse-selection protection
        (reinforced by the 2026-08-06 cap audit: widening its selection at
        constant gross degrades it). Any evaluation of a retired KIND under
        an auction fill model is a NEW trial (ledgers/trials.yml) per the
        doctrine artifact's norm.
        """
        if quantity <= 0:
            raise BrokerOrderError(f"quantity must be positive; got {quantity}")
        if limit_price <= 0:
            raise BrokerOrderError(f"limit_price must be positive; got {limit_price}")

        try:
            from tigeropen.common.util.order_utils import auction_limit_order
        except ImportError as exc:
            raise BrokerOrderError("tigeropen SDK not installed") from exc

        try:
            contract = self._tc.get_contract(symbol=symbol)
        except Exception as exc:
            raise BrokerOrderError(f"get_contract({symbol}) failed: {exc}") from exc
        if contract is None:
            raise BrokerOrderError(f"no contract found for symbol {symbol}")

        order = auction_limit_order(
            account=self._account,
            contract=contract,
            action="BUY",
            quantity=quantity,
            limit_price=limit_price,
        )
        if user_mark is not None:
            try:
                order.user_mark = str(user_mark)[:32]
            except Exception:  # noqa: BLE001
                pass
        try:
            order_id = self._tc.place_order(order)
        except Exception as exc:
            raise BrokerOrderError(
                f"place_order(AL BUY {quantity} {symbol} @ {limit_price}) failed: {exc}"
            ) from exc

        return TraceEntry(
            tool=TOOL,
            inputs={
                "call": "place_auction_limit",
                "symbol": symbol,
                "action": "BUY",
                "quantity": quantity,
                "limit_price": limit_price,
                "account_masked": self._config_info["account_masked"],
            },
            output={
                "order_id": int(order_id) if order_id is not None else None,
                "symbol": symbol,
                "action": "BUY",
                "quantity": quantity,
                "limit_price": limit_price,
                "order_type": "AL",
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

        # Absence-of-evidence guard (broadened): None or any non-list is an
        # unconfirmed soft-failure, not "no fills". Raise so reconcile fails safe.
        if not isinstance(raw, (list, tuple)):
            raise BrokerOrderError(
                f"get_filled_orders returned {type(raw).__name__} (unconfirmed) — "
                f"refusing to treat as empty"
            )
        orders = []
        for o in raw:
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
                "user_mark": getattr(o, "user_mark", None),
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
