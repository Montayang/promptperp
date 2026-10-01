from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Mapping

from promptperp.accounting.errors import AccountingValidationError

BASE_ASSET = "USDT"
MONEY_QUANTUM = Decimal("0.00000001")
UNIT_QUANTUM = Decimal("0.000000000000000001")


def require_utc(value: datetime, field: str = "timestamp") -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AccountingValidationError(f"{field} must be timezone-aware")


def require_identifier(value: str, field: str) -> None:
    if not value or len(value) > 128:
        raise AccountingValidationError(f"{field} is required and bounded")


def require_money(value: Decimal, field: str, *, positive: bool = False) -> None:
    if not value.is_finite():
        raise AccountingValidationError(f"{field} must be finite")
    if positive and value <= 0:
        raise AccountingValidationError(f"{field} must be positive")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise AccountingValidationError(f"{field} has invalid exponent")
    if exponent < -8:
        raise AccountingValidationError(f"{field} exceeds money precision")


class EntryDirection(str, Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class ReconciliationStatus(str, Enum):
    PASSED = "PASSED"
    BLOCKED = "BLOCKED"


class ReportFrequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass(frozen=True)
class Posting:
    account: str
    direction: EntryDirection
    amount: Decimal
    asset: str = BASE_ASSET

    def __post_init__(self) -> None:
        require_identifier(self.account, "account")
        require_money(self.amount, "posting amount", positive=True)
        if self.asset != BASE_ASSET:
            raise AccountingValidationError("only USDT postings are supported")


@dataclass(frozen=True)
class LedgerTransaction:
    transaction_id: str
    kind: str
    occurred_at: datetime
    actor: str
    reason: str
    external_reference: str
    postings: tuple[Posting, ...]

    def __post_init__(self) -> None:
        for field, value in (
            ("transaction_id", self.transaction_id),
            ("kind", self.kind),
            ("actor", self.actor),
            ("reason", self.reason),
            ("external_reference", self.external_reference),
        ):
            require_identifier(value, field)
        require_utc(self.occurred_at, "occurred_at")
        if len(self.postings) < 2:
            raise AccountingValidationError("transaction needs at least two postings")
        debits = sum(
            (
                item.amount
                for item in self.postings
                if item.direction is EntryDirection.DEBIT
            ),
            Decimal("0"),
        )
        credits = sum(
            (
                item.amount
                for item in self.postings
                if item.direction is EntryDirection.CREDIT
            ),
            Decimal("0"),
        )
        if debits != credits:
            raise AccountingValidationError("transaction postings are not balanced")


@dataclass(frozen=True)
class CashFlowGate:
    checked_at: datetime
    reconciled_at: datetime | None
    reconciliation_id: str | None = None
    has_managed_position: bool = False
    has_foreign_position: bool = False
    has_open_order: bool = False
    has_unsettled_event: bool = False
    has_unknown_result: bool = False
    unexplained_difference: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        require_utc(self.checked_at, "checked_at")
        if self.reconciled_at is not None:
            require_utc(self.reconciled_at, "reconciled_at")
        if self.reconciliation_id is not None:
            require_identifier(self.reconciliation_id, "reconciliation_id")
        require_money(self.unexplained_difference, "unexplained_difference")


@dataclass(frozen=True)
class TradingSettlement:
    event_id: str
    occurred_at: datetime
    strategy_id: str
    run_id: str
    intent_id: str
    order_id: str
    trade_id: str
    realized_pnl: Decimal
    commission: Decimal
    funding: Decimal
    asset: str = BASE_ASSET
    schema_version: int = 1

    def __post_init__(self) -> None:
        for field in (
            "event_id",
            "strategy_id",
            "run_id",
            "intent_id",
            "order_id",
            "trade_id",
        ):
            require_identifier(str(getattr(self, field)), field)
        require_utc(self.occurred_at, "occurred_at")
        require_money(self.realized_pnl, "realized_pnl")
        require_money(self.commission, "commission")
        require_money(self.funding, "funding")
        if self.commission < 0:
            raise AccountingValidationError("commission cannot be negative")
        if self.asset != BASE_ASSET:
            raise AccountingValidationError("non-USDT settlement is unsupported")
        if self.schema_version != 1:
            raise AccountingValidationError("unsupported settlement schema")

    @property
    def net_change(self) -> Decimal:
        return self.realized_pnl - self.commission + self.funding


@dataclass(frozen=True)
class ReconciliationResult:
    reconciliation_id: str
    occurred_at: datetime
    exchange_equity: Decimal
    internal_equity: Decimal
    difference: Decimal
    status: ReconciliationStatus
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ValuationSnapshot:
    snapshot_id: str
    pool_id: str
    occurred_at: datetime
    pool_equity: Decimal
    outstanding_units: Decimal
    unit_nav: Decimal
    reconciliation_id: str


@dataclass(frozen=True)
class InvestorView:
    investor_id: str
    display_name: str
    pool_id: str
    strategy_id: str
    strategy_version: str
    units: Decimal
    unit_nav: Decimal
    equity: Decimal
    net_contributions: Decimal
    return_rate: Decimal
    snapshot_at: datetime
    reconciliation_id: str


@dataclass(frozen=True)
class SourceEvent:
    event_id: str
    event_type: str
    occurred_at: datetime
    payload: Mapping[str, str]
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_identifier(self.event_id, "event_id")
        require_identifier(self.event_type, "event_type")
        require_utc(self.occurred_at, "occurred_at")
        if self.schema_version != 1:
            raise AccountingValidationError("unsupported source event schema")
        if not isinstance(self.payload, Mapping):
            raise AccountingValidationError("source event payload must be a mapping")
