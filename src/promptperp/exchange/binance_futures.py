from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from promptperp.config import RuntimeConfig
from promptperp.domain import (
    AccountBalance,
    FuturesPosition,
    Order,
    OrderSide,
    OrderStatus,
    PositionSide,
    RequestRejected,
    RequestUnknown,
    ResponseShapeError,
    SymbolRules,
)


class BinanceFuturesAdapter:
    """Normalize Binance SDK responses behind stable, fail-closed interfaces."""

    def __init__(self, *, rest_api: Any, runtime_config: RuntimeConfig | None = None):
        self._rest_api = rest_api
        self._runtime_config = runtime_config or RuntimeConfig()
        self._rules_cache: dict[str, SymbolRules] = {}

    def update_runtime_config(self, runtime_config: RuntimeConfig) -> None:
        """Replace the immutable approval after a supervised universe refresh."""

        self._runtime_config = runtime_config

    def get_last_price(self, symbol: str) -> Decimal:
        response = self._read(
            "ticker price",
            lambda: self._rest_api.ticker_price(symbol=symbol.upper()),
        )
        data = self._mapping(self._response_data(response), "ticker price")
        return self._positive_decimal(data.get("price"), "ticker price")

    def get_symbol_rules(self, symbol: str) -> SymbolRules:
        normalized_symbol = symbol.upper()
        cached = self._rules_cache.get(normalized_symbol)
        if cached is not None:
            return cached

        response = self._read(
            "exchange information",
            self._rest_api.exchange_information,
        )
        payload = self._mapping(self._response_data(response), "exchange information")
        symbols = payload.get("symbols")
        if not isinstance(symbols, list):
            raise ResponseShapeError("exchange information is missing symbols")
        symbol_data = next(
            (
                self._mapping(item, "symbol rules")
                for item in symbols
                if self._mapping(item, "symbol rules").get("symbol")
                == normalized_symbol
            ),
            None,
        )
        if symbol_data is None:
            raise ResponseShapeError("requested symbol rules are unavailable")
        raw_filters = symbol_data.get("filters")
        if not isinstance(raw_filters, list):
            raise ResponseShapeError("symbol rules are missing filters")
        filters = {
            item.get("filterType"): item
            for raw in raw_filters
            for item in [self._mapping(raw, "symbol filter")]
        }
        quantity_filter = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE")
        price_filter = filters.get("PRICE_FILTER")
        notional_filter = filters.get("MIN_NOTIONAL", {})
        if not quantity_filter or not price_filter:
            raise ResponseShapeError("symbol quantity or price rules are incomplete")

        rules = SymbolRules(
            symbol=normalized_symbol,
            quantity_step=self._decimal(quantity_filter.get("stepSize"), "step size"),
            minimum_quantity=self._decimal(
                quantity_filter.get("minQty"), "minimum quantity"
            ),
            price_tick=self._decimal(price_filter.get("tickSize"), "price tick"),
            minimum_notional=self._decimal(
                notional_filter.get("notional", notional_filter.get("minNotional", 0)),
                "minimum notional",
            ),
        )
        self._rules_cache[normalized_symbol] = rules
        return rules

    def list_positions(self) -> tuple[FuturesPosition, ...]:
        response = self._read(
            "position query",
            self._rest_api.position_information_v3,
        )
        positions = []
        for raw in self._rows(response, "position query"):
            data = self._mapping(raw, "position")
            amount = self._decimal(
                data.get("positionAmt", data.get("position_amt")),
                "position amount",
            )
            if amount == 0:
                continue
            raw_side = data.get("positionSide", data.get("position_side"))
            if raw_side in {"LONG", "SHORT"}:
                side = PositionSide(raw_side)
            else:
                side = PositionSide.LONG if amount > 0 else PositionSide.SHORT
            symbol = data.get("symbol")
            if not isinstance(symbol, str) or not symbol:
                raise ResponseShapeError("active position is missing symbol")
            positions.append(
                FuturesPosition(
                    symbol=symbol,
                    side=side,
                    quantity=abs(amount),
                    entry_price=self._decimal(
                        data.get("entryPrice", data.get("entry_price", 0)),
                        "entry price",
                    ),
                )
            )
        return tuple(positions)

    def get_balance(self, asset: str) -> AccountBalance:
        response = self._read(
            "balance query",
            self._rest_api.futures_account_balance_v3,
        )
        normalized_asset = asset.upper()
        for raw in self._rows(response, "balance query"):
            data = self._mapping(raw, "balance")
            if data.get("asset") != normalized_asset:
                continue
            return AccountBalance(
                asset=normalized_asset,
                wallet_balance=self._decimal(data.get("balance"), "wallet balance"),
                available_balance=self._decimal(
                    data.get("availableBalance", data.get("available_balance")),
                    "available balance",
                ),
            )
        raise ResponseShapeError("requested account balance is unavailable")

    def is_hedge_mode(self) -> bool:
        response = self._read(
            "position mode query",
            self._rest_api.get_current_position_mode,
        )
        data = self._mapping(self._response_data(response), "position mode")
        value = data.get("dualSidePosition", data.get("dual_side_position"))
        if not isinstance(value, bool):
            raise ResponseShapeError("position mode is missing or invalid")
        return value

    def place_market_order(
        self,
        *,
        symbol: str,
        side: OrderSide,
        position_side: PositionSide,
        quantity: Decimal,
        client_order_id: str,
    ) -> Order:
        if not client_order_id:
            raise RequestRejected("client order id is required")
        if quantity <= 0:
            raise RequestRejected("order quantity must be positive")
        self._runtime_config.require_account_mutation(
            "place futures market order",
            symbol=symbol,
            side=side.value,
        )
        try:
            response = self._rest_api.new_order(
                symbol=symbol.upper(),
                side=side.value,
                type="MARKET",
                quantity=str(quantity),
                position_side=position_side.value,
                new_client_order_id=client_order_id,
                new_order_resp_type="RESULT",
            )
        except Exception as exc:
            raise RequestUnknown("market-order outcome is unknown") from exc
        try:
            order = self._order(response, "market order")
        except ResponseShapeError:
            data = self._mapping(self._response_data(response), "market order")
            order_id = data.get("orderId", data.get("order_id"))
            if data.get("status") != OrderStatus.FILLED.value or order_id in (
                None,
                "",
            ):
                raise
            try:
                order = self.get_order(symbol=symbol, order_id=str(order_id))
            except (RequestUnknown, ResponseShapeError) as refresh_exc:
                raise RequestUnknown(
                    "filled market-order execution details remain incomplete"
                ) from refresh_exc
        if order.status is OrderStatus.FILLED and (
            order.executed_quantity <= 0 or order.average_price <= 0
        ):
            # Binance can acknowledge a RESULT market order as FILLED before the
            # response body contains its final execution fields.  Never pass that
            # transient shape to the durable state machine: reconcile the known
            # order identity first, using a read that is safe to retry.
            try:
                order = self.get_order(symbol=symbol, order_id=order.order_id)
            except (RequestUnknown, ResponseShapeError) as exc:
                raise RequestUnknown(
                    "filled market-order execution details remain incomplete"
                ) from exc
            if order.status is OrderStatus.FILLED and (
                order.executed_quantity <= 0 or order.average_price <= 0
            ):
                raise RequestUnknown(
                    "filled market-order execution details remain incomplete"
                )
        if order.status is OrderStatus.REJECTED:
            raise RequestRejected("market order was rejected")
        return order

    def get_order(
        self,
        *,
        symbol: str,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> Order:
        if bool(order_id) == bool(client_order_id):
            raise RequestRejected(
                "provide exactly one exchange order id or client order id"
            )
        identifiers: dict[str, str]
        if order_id is not None:
            identifiers = {"order_id": order_id}
        else:
            assert client_order_id is not None
            identifiers = {"orig_client_order_id": client_order_id}
        response = self._read(
            "order query",
            lambda: self._rest_api.query_order(symbol=symbol.upper(), **identifiers),
        )
        return self._order(response, "order query")

    def cancel_order(self, *, symbol: str, order_id: str) -> None:
        if not order_id:
            raise RequestRejected("order id is required")
        self._runtime_config.require_account_mutation(
            "cancel futures order", symbol=symbol
        )
        try:
            self._rest_api.cancel_order(symbol=symbol.upper(), order_id=order_id)
        except Exception as exc:
            raise RequestUnknown("cancel-order outcome is unknown") from exc

    def _order(self, response: Any, context: str) -> Order:
        data = self._mapping(self._response_data(response), context)
        try:
            status = OrderStatus(data.get("status", "UNKNOWN"))
        except ValueError:
            status = OrderStatus.UNKNOWN
        order_id = data.get("orderId", data.get("order_id"))
        if order_id in (None, ""):
            raise ResponseShapeError(f"{context} is missing order id")
        try:
            return Order(
                symbol=self._required_text(data, "symbol", context),
                order_id=str(order_id),
                client_order_id=data.get("clientOrderId", data.get("client_order_id")),
                side=OrderSide(self._required_text(data, "side", context)),
                position_side=PositionSide(
                    data.get("positionSide", data.get("position_side"))
                ),
                status=status,
                requested_quantity=self._positive_decimal(
                    data.get("origQty", data.get("orig_qty", data.get("quantity"))),
                    "requested quantity",
                ),
                executed_quantity=self._decimal(
                    data.get("executedQty", data.get("executed_qty", 0)),
                    "executed quantity",
                ),
                average_price=self._decimal(
                    data.get("avgPrice", data.get("avg_price", 0)),
                    "average price",
                ),
            )
        except ResponseShapeError:
            raise
        except (TypeError, ValueError) as exc:
            raise ResponseShapeError(f"{context} contains invalid order data") from exc

    @staticmethod
    def _response_data(response: Any) -> Any:
        value = getattr(response, "data", response)
        return value() if callable(value) else value

    @classmethod
    def _rows(cls, response: Any, context: str) -> list[Any]:
        value = cls._response_data(response)
        if value is None:
            raise ResponseShapeError(f"{context} returned no data")
        if isinstance(value, list):
            return value
        if hasattr(value, "to_dict"):
            value = value.to_dict()
            if isinstance(value, list):
                return value
        return [value]

    @staticmethod
    def _mapping(value: Any, context: str) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if hasattr(value, "to_dict"):
            converted = value.to_dict()
            if isinstance(converted, dict):
                return converted
        raise ResponseShapeError(f"{context} has an unexpected response shape")

    @staticmethod
    def _decimal(value: Any, field: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except Exception as exc:
            raise ResponseShapeError(f"{field} is missing or invalid") from exc
        if not result.is_finite():
            raise ResponseShapeError(f"{field} is not finite")
        return result

    @classmethod
    def _positive_decimal(cls, value: Any, field: str) -> Decimal:
        result = cls._decimal(value, field)
        if result <= 0:
            raise ResponseShapeError(f"{field} must be positive")
        return result

    @staticmethod
    def _required_text(data: dict[str, Any], field: str, context: str) -> str:
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise ResponseShapeError(f"{context} is missing {field}")
        return value

    @staticmethod
    def _read(context: str, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except (RequestUnknown, ResponseShapeError):
            raise
        except Exception as exc:
            raise RequestUnknown(f"{context} failed") from exc
