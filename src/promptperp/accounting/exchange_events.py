from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from promptperp.accounting.errors import AccountingValidationError


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise AccountingValidationError(f"{field} must use UTC")


class OwnedOrderRole(str, Enum):
    ENTRY = "ENTRY"
    STOP = "STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    EXIT = "EXIT"


@dataclass(frozen=True)
class ExchangeTrade:
    symbol: str
    trade_id: str
    order_id: str
    occurred_at: datetime
    realized_pnl: Decimal
    commission: Decimal
    commission_asset: str

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper():
            raise AccountingValidationError("trade symbol must be uppercase")
        if not self.trade_id or not self.order_id:
            raise AccountingValidationError("trade identity is required")
        _require_utc(self.occurred_at, "trade occurred_at")
        if not self.realized_pnl.is_finite():
            raise AccountingValidationError("trade realized PnL must be finite")
        if not self.commission.is_finite() or self.commission < 0:
            raise AccountingValidationError("trade commission must be non-negative")
        if self.commission_asset != "USDT":
            raise AccountingValidationError("non-USDT commission is unsupported")

    @property
    def event_id(self) -> str:
        return f"binance:trade:{self.symbol}:{self.trade_id}"


@dataclass(frozen=True)
class ExchangeFunding:
    symbol: str
    transaction_id: str
    occurred_at: datetime
    amount: Decimal
    asset: str

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper():
            raise AccountingValidationError("funding symbol must be uppercase")
        if not self.transaction_id:
            raise AccountingValidationError("funding transaction identity is required")
        _require_utc(self.occurred_at, "funding occurred_at")
        if not self.amount.is_finite():
            raise AccountingValidationError("funding amount must be finite")
        if self.asset != "USDT":
            raise AccountingValidationError("non-USDT funding is unsupported")

    @property
    def event_id(self) -> str:
        return f"binance:funding:{self.transaction_id}"


@dataclass(frozen=True)
class ShadowSyncReport:
    started_at: datetime
    completed_at: datetime
    verified_events: int
    posted_events: int
    duplicate_events: int
    quarantined_events: int

    @property
    def passed(self) -> bool:
        return self.quarantined_events == 0


@dataclass(frozen=True)
class OwnedSettlementTotals:
    intent_id: str
    realized_pnl: Decimal
    commission: Decimal
    funding: Decimal
    event_count: int

    def __post_init__(self) -> None:
        if not self.intent_id:
            raise AccountingValidationError("settlement intent ID is required")
        if not all(
            amount.is_finite()
            for amount in (self.realized_pnl, self.commission, self.funding)
        ):
            raise AccountingValidationError("settlement totals must be finite")
        if self.commission < 0:
            raise AccountingValidationError("settlement commission cannot be negative")
        if self.event_count < 0:
            raise AccountingValidationError("settlement event count cannot be negative")

    @property
    def net_amount(self) -> Decimal:
        return self.realized_pnl - self.commission + self.funding
