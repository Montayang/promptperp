from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from promptperp.domain import (
    AccountBalance,
    FuturesPosition,
    Order,
    OrderSide,
    PositionSide,
    SymbolRules,
)


class MarketDataGateway(Protocol):
    def get_last_price(self, symbol: str) -> Decimal: ...

    def get_symbol_rules(self, symbol: str) -> SymbolRules: ...


class AccountGateway(Protocol):
    def list_positions(self) -> tuple[FuturesPosition, ...]: ...

    def get_balance(self, asset: str) -> AccountBalance: ...

    def is_hedge_mode(self) -> bool: ...


class ExecutionGateway(Protocol):
    def place_market_order(
        self,
        *,
        symbol: str,
        side: OrderSide,
        position_side: PositionSide,
        quantity: Decimal,
        client_order_id: str,
    ) -> Order: ...

    def get_order(
        self,
        *,
        symbol: str,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> Order: ...

    def cancel_order(self, *, symbol: str, order_id: str) -> None: ...
