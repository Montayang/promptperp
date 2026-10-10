from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from enum import Enum


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    quantity_step: Decimal
    minimum_quantity: Decimal
    price_tick: Decimal
    minimum_notional: Decimal

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        if self.quantity_step <= 0 or self.price_tick <= 0:
            raise ValueError("quantity step and price tick must be positive")
        if self.minimum_quantity < 0 or self.minimum_notional < 0:
            raise ValueError("minimum quantity and notional cannot be negative")

    def floor_quantity(self, quantity: Decimal) -> Decimal:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        result = (quantity / self.quantity_step).to_integral_value(
            rounding=ROUND_DOWN
        ) * self.quantity_step
        if result < self.minimum_quantity:
            raise ValueError("quantity is below the symbol minimum")
        return result

    def floor_price(self, price: Decimal) -> Decimal:
        if price <= 0:
            raise ValueError("price must be positive")
        return (price / self.price_tick).to_integral_value(
            rounding=ROUND_DOWN
        ) * self.price_tick

    def validate_notional(self, quantity: Decimal, price: Decimal) -> None:
        if quantity * price < self.minimum_notional:
            raise ValueError("notional is below the symbol minimum")


@dataclass(frozen=True)
class FuturesPosition:
    symbol: str
    side: PositionSide
    quantity: Decimal
    entry_price: Decimal

    def __post_init__(self) -> None:
        if not self.symbol or self.quantity <= 0 or self.entry_price < 0:
            raise ValueError("invalid futures position")


@dataclass(frozen=True)
class AccountBalance:
    asset: str
    wallet_balance: Decimal
    available_balance: Decimal

    def __post_init__(self) -> None:
        if not self.asset or self.wallet_balance < 0 or self.available_balance < 0:
            raise ValueError("invalid account balance")


@dataclass(frozen=True)
class Order:
    symbol: str
    order_id: str
    client_order_id: str | None
    side: OrderSide
    position_side: PositionSide
    status: OrderStatus
    requested_quantity: Decimal
    executed_quantity: Decimal
    average_price: Decimal

    def __post_init__(self) -> None:
        if not self.symbol or not self.order_id or self.requested_quantity <= 0:
            raise ValueError("invalid order identity or quantity")
        if (
            self.executed_quantity < 0
            or self.executed_quantity > self.requested_quantity
        ):
            raise ValueError("executed quantity is outside the requested quantity")
        if self.average_price < 0:
            raise ValueError("average price cannot be negative")
        if (
            self.status is OrderStatus.FILLED
            and self.executed_quantity != self.requested_quantity
        ):
            raise ValueError("filled order must execute the requested quantity")
        if self.status is OrderStatus.PARTIALLY_FILLED and not (
            Decimal("0") < self.executed_quantity < self.requested_quantity
        ):
            raise ValueError("partial order must have an intermediate fill quantity")


@dataclass(frozen=True)
class ProtectionClose:
    protection_client_order_id: str
    order: Order

    def __post_init__(self) -> None:
        if not self.protection_client_order_id:
            raise ValueError("protection client order identity is required")


@dataclass(frozen=True)
class Fill:
    symbol: str
    order_id: str
    trade_id: str
    side: OrderSide
    position_side: PositionSide
    quantity: Decimal
    price: Decimal
    commission: Decimal
    commission_asset: str

    def __post_init__(self) -> None:
        if not self.symbol or not self.order_id or not self.trade_id:
            raise ValueError("invalid fill identity")
        if self.quantity <= 0 or self.price <= 0 or self.commission < 0:
            raise ValueError("invalid fill amount")
        if not self.commission_asset:
            raise ValueError("commission asset is required")


@dataclass(frozen=True)
class FuturesTradeFill:
    fill: Fill
    occurred_at: datetime
    realized_pnl: Decimal

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            raise ValueError("fill time must be timezone-aware")
        if not self.realized_pnl.is_finite():
            raise ValueError("fill realized PnL must be finite")

    @property
    def event_id(self) -> str:
        return f"binance:fill:{self.fill.symbol}:{self.fill.trade_id}"
