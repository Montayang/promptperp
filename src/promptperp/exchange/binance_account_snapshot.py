from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from promptperp.domain import (
    AccountBalance,
    FuturesPosition,
    OrderSide,
    PositionSide,
    RequestUnknown,
    ResponseShapeError,
)
from promptperp.exchange.binance_futures import BinanceFuturesAdapter


@dataclass(frozen=True)
class PhysicalOpenOrder:
    symbol: str
    client_order_id: str
    side: OrderSide
    position_side: PositionSide
    quantity: Decimal
    is_conditional: bool

    def __post_init__(self) -> None:
        if (
            not self.symbol
            or self.symbol != self.symbol.upper()
            or not self.client_order_id
            or not self.quantity.is_finite()
            or self.quantity <= 0
        ):
            raise ValueError("physical open-order fields are invalid")


@dataclass(frozen=True)
class PhysicalAccountSnapshot:
    observed_at: datetime
    balance: AccountBalance
    hedge_mode: bool
    positions: tuple[FuturesPosition, ...]
    open_orders: tuple[PhysicalOpenOrder, ...]

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("physical account snapshot time must be timezone-aware")
        if not isinstance(self.hedge_mode, bool):
            raise ValueError("physical account position mode must be explicit")
        object.__setattr__(self, "positions", tuple(self.positions))
        object.__setattr__(self, "open_orders", tuple(self.open_orders))


class BinanceAccountSnapshotReader:
    """Read one fail-closed USD-M account snapshot without mutating the account."""

    def __init__(self, *, rest_api: Any):
        self._rest_api = rest_api
        self._futures = BinanceFuturesAdapter(rest_api=rest_api)

    def read(self, *, observed_at: datetime) -> PhysicalAccountSnapshot:
        if observed_at.tzinfo is None:
            raise ValueError("account snapshot time must be timezone-aware")
        hedge_mode = self._futures.is_hedge_mode()
        positions = self._futures.list_positions()
        balance = self._futures.get_balance("USDT")
        normal = self._orders(
            self._read(
                "normal open-order query", self._rest_api.current_all_open_orders
            ),
            conditional=False,
        )
        conditional = self._orders(
            self._read(
                "conditional open-order query",
                self._rest_api.current_all_algo_open_orders,
            ),
            conditional=True,
        )
        orders = normal + conditional
        identities = [item.client_order_id for item in orders]
        if len(set(identities)) != len(identities):
            raise ResponseShapeError("open-order client identity is duplicated")
        return PhysicalAccountSnapshot(
            observed_at=observed_at,
            balance=balance,
            hedge_mode=hedge_mode,
            positions=positions,
            open_orders=tuple(
                sorted(
                    orders,
                    key=lambda item: (
                        item.symbol,
                        item.position_side.value,
                        item.client_order_id,
                    ),
                )
            ),
        )

    def _orders(
        self, response: Any, *, conditional: bool
    ) -> tuple[PhysicalOpenOrder, ...]:
        context = "conditional open order" if conditional else "normal open order"
        rows = BinanceFuturesAdapter._rows(response, context)
        return tuple(self._order(row, conditional=conditional) for row in rows)

    @staticmethod
    def _order(row: Any, *, conditional: bool) -> PhysicalOpenOrder:
        context = "conditional open order" if conditional else "normal open order"
        value = BinanceFuturesAdapter._mapping(row, context)
        client_field = "clientAlgoId" if conditional else "clientOrderId"
        client_fallback = "client_algo_id" if conditional else "client_order_id"
        quantity = value.get(
            "quantity",
            value.get("origQty", value.get("orig_qty")),
        )
        try:
            symbol = value["symbol"]
            client_order_id = value.get(client_field, value.get(client_fallback))
            if not isinstance(symbol, str) or not isinstance(client_order_id, str):
                raise ValueError("order identity must be text")
            side = OrderSide(str(value["side"]).upper())
            position_side = PositionSide(
                str(value.get("positionSide", value.get("position_side"))).upper()
            )
            normalized_quantity = Decimal(str(quantity))
        except (KeyError, TypeError, ValueError) as exc:
            raise ResponseShapeError(f"{context} is missing required fields") from exc
        try:
            return PhysicalOpenOrder(
                symbol=symbol,
                client_order_id=client_order_id,
                side=side,
                position_side=position_side,
                quantity=normalized_quantity,
                is_conditional=conditional,
            )
        except ValueError as exc:
            raise ResponseShapeError(f"{context} contains invalid fields") from exc

    @staticmethod
    def _read(context: str, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except (RequestUnknown, ResponseShapeError):
            raise
        except Exception as exc:
            raise RequestUnknown(f"{context} failed") from exc
