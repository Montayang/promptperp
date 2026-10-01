from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from promptperp.domain import OrderSide, PositionSide


class ExecutionState(str, Enum):
    INTENT = "INTENT"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PROTECTING = "PROTECTING"
    PROTECTED = "PROTECTED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    SETTLED = "SETTLED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class TradeIntent:
    strategy_id: str
    run_id: str
    intent_id: str
    symbol: str
    side: OrderSide
    position_side: PositionSide
    quantity: Decimal

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.run_id or not self.intent_id:
            raise ValueError("intent ownership identifiers are required")
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("intent symbol must be uppercase")
        if self.quantity <= 0 or not self.quantity.is_finite():
            raise ValueError("intent quantity must be finite and positive")


@dataclass(frozen=True)
class ExecutionEvent:
    schema_version: int
    sequence: int
    occurred_at: datetime
    strategy_id: str
    run_id: str
    intent_id: str
    event_type: str
    payload: Mapping[str, Any]
    previous_hash: str
    record_hash: str


@dataclass(frozen=True)
class ExecutionSnapshot:
    intent: TradeIntent
    state: ExecutionState
    entry_client_order_id: str
    exchange_order_id: str | None = None
    filled_quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    stop_client_order_id: str | None = None
    take_profit_client_order_id: str | None = None
    stop_exchange_order_id: str | None = None
    take_profit_exchange_order_id: str | None = None
    close_client_order_id: str | None = None
    close_exchange_order_id: str | None = None
    close_filled_quantity: Decimal = Decimal("0")
    close_average_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    funding: Decimal = Decimal("0")
    last_reason: str = ""
    event_count: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        amounts = (
            self.filled_quantity,
            self.average_price,
            self.close_filled_quantity,
            self.close_average_price,
            self.realized_pnl,
            self.commission,
            self.funding,
        )
        if not all(value.is_finite() for value in amounts):
            raise ValueError("execution amounts must be finite")
        if not Decimal("0") <= self.filled_quantity <= self.intent.quantity:
            raise ValueError("filled quantity is outside the intent quantity")
        if self.average_price < 0 or self.commission < 0:
            raise ValueError("average price and commission cannot be negative")
        if not Decimal("0") <= self.close_filled_quantity <= self.filled_quantity:
            raise ValueError("close filled quantity is outside the owned quantity")
        if self.close_average_price < 0:
            raise ValueError("close average price cannot be negative")
        position_states = {
            ExecutionState.FILLED,
            ExecutionState.PROTECTING,
            ExecutionState.PROTECTED,
            ExecutionState.CLOSING,
            ExecutionState.CLOSED,
            ExecutionState.SETTLED,
        }
        if self.state in position_states and (
            self.filled_quantity <= 0 or self.average_price <= 0
        ):
            raise ValueError(
                "filled execution requires positive fill and average price"
            )
        if self.state is ExecutionState.PROTECTING and not all(
            (self.stop_client_order_id, self.take_profit_client_order_id)
        ):
            raise ValueError("protecting state requires protection client identities")
        if self.state is ExecutionState.PROTECTED and not all(
            (
                self.stop_client_order_id,
                self.take_profit_client_order_id,
                self.stop_exchange_order_id,
                self.take_profit_exchange_order_id,
            )
        ):
            raise ValueError("protected state requires confirmed protection identities")
        if (
            self.state
            in {
                ExecutionState.CLOSING,
                ExecutionState.CLOSED,
                ExecutionState.SETTLED,
            }
            and not self.close_client_order_id
        ):
            raise ValueError("closing execution requires a close client identity")
        if self.state in {ExecutionState.CLOSED, ExecutionState.SETTLED} and (
            not self.close_exchange_order_id
            or self.close_filled_quantity != self.filled_quantity
            or self.close_average_price <= 0
        ):
            raise ValueError(
                "closed execution requires a confirmed complete close fill"
            )
        if not isinstance(self.metadata, Mapping):
            raise ValueError("execution metadata must be a mapping")

    @property
    def owns_position(self) -> bool:
        return (
            self.state
            in {
                ExecutionState.FILLED,
                ExecutionState.PROTECTING,
                ExecutionState.PROTECTED,
                ExecutionState.CLOSING,
            }
            and self.owned_quantity > 0
        )

    @property
    def owned_quantity(self) -> Decimal:
        return self.filled_quantity - self.close_filled_quantity
