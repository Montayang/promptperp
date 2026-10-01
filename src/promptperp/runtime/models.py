from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Mapping

from promptperp.domain import OrderSide, PositionSide


class PlatformMode(str, Enum):
    NORMAL = "NORMAL"
    REDUCE_ONLY = "REDUCE_ONLY"
    KILL_SWITCH = "KILL_SWITCH"


class PlatformRunState(str, Enum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    STOP_REQUESTED = "STOP_REQUESTED"
    STOPPED = "STOPPED"
    BLOCKED = "BLOCKED"
    BLOCKED_STOP_REQUESTED = "BLOCKED_STOP_REQUESTED"


class ClaimState(str, Enum):
    RESERVED = "RESERVED"
    OPEN = "OPEN"


@dataclass(frozen=True)
class StrategyDescriptor:
    strategy_id: str
    strategy_version: str
    interface_version: int
    parameter_schema_version: int
    event_kind: str
    parameter_schema: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.strategy_version or not self.event_kind:
            raise ValueError("strategy descriptor identity is required")
        if self.interface_version != 1 or self.parameter_schema_version <= 0:
            raise ValueError("strategy descriptor version is unsupported")
        object.__setattr__(
            self,
            "parameter_schema",
            MappingProxyType(
                {
                    key: MappingProxyType(dict(value))
                    for key, value in self.parameter_schema.items()
                }
            ),
        )

    @property
    def fingerprint(self) -> str:
        values = {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "interface_version": self.interface_version,
            "parameter_schema_version": self.parameter_schema_version,
            "event_kind": self.event_kind,
            "parameter_schema": {
                key: dict(value) for key, value in self.parameter_schema.items()
            },
        }
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
        return sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class SignalProposal:
    strategy_id: str
    symbol: str
    side: OrderSide
    position_side: PositionSide
    margin: Decimal
    leverage: int
    take_profit_ratio: Decimal
    stop_loss_ratio: Decimal
    observed_at: datetime
    reason: str

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.symbol or not self.reason:
            raise ValueError("signal proposal identity is required")
        if self.symbol != self.symbol.upper():
            raise ValueError("signal proposal symbol must be uppercase")
        if self.observed_at.tzinfo is None:
            raise ValueError("signal proposal time must be timezone-aware")
        values = (self.margin, self.take_profit_ratio, self.stop_loss_ratio)
        if not all(value.is_finite() and value > 0 for value in values):
            raise ValueError("signal proposal amounts must be finite and positive")
        if self.leverage <= 0:
            raise ValueError("signal proposal leverage must be positive")
        if self.take_profit_ratio >= 1 or self.stop_loss_ratio >= 1:
            raise ValueError("signal proposal protection ratios must be below one")


@dataclass(frozen=True)
class RunPlan:
    strategy_id: str
    run_id: str
    plugin_fingerprint: str
    parameter_fingerprint: str
    parameters: Mapping[str, Any]
    capital_budget: Decimal
    max_margin_per_trade: Decimal
    max_leverage: int
    max_loss_per_trade: Decimal
    max_positions: int
    created_at: datetime

    def __post_init__(self) -> None:
        if not all(
            (
                self.strategy_id,
                self.run_id,
                self.plugin_fingerprint,
                self.parameter_fingerprint,
            )
        ):
            raise ValueError("run plan identity is required")
        if not re.fullmatch(
            r"[0-9a-f]{64}", self.plugin_fingerprint
        ) or not re.fullmatch(r"[0-9a-f]{64}", self.parameter_fingerprint):
            raise ValueError("run plan fingerprints are invalid")
        if self.created_at.tzinfo is None:
            raise ValueError("run plan time must be timezone-aware")
        if self.capital_budget <= 0 or self.max_margin_per_trade <= 0:
            raise ValueError("run plan budgets must be positive")
        if self.max_margin_per_trade > self.capital_budget:
            raise ValueError("per-trade margin cannot exceed run capital budget")
        if self.max_leverage <= 0 or self.max_positions <= 0:
            raise ValueError("run leverage and position limits must be positive")
        if self.max_loss_per_trade <= 0:
            raise ValueError("run loss limit must be positive")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True)
class PositionClaim:
    strategy_id: str
    run_id: str
    symbol: str
    position_side: PositionSide
    margin: Decimal
    quantity: Decimal
    state: ClaimState
    protected: bool
    claimed_at: datetime

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.run_id or not self.symbol:
            raise ValueError("position claim identity is required")
        if self.symbol != self.symbol.upper():
            raise ValueError("position claim symbol must be uppercase")
        if self.margin <= 0 or self.quantity <= 0:
            raise ValueError("position claim exposure must be positive")
        if self.claimed_at.tzinfo is None:
            raise ValueError("position claim time must be timezone-aware")
        if self.state is ClaimState.RESERVED and self.protected:
            raise ValueError("a reserved position cannot claim protection")


@dataclass(frozen=True)
class AccountPosition:
    symbol: str
    position_side: PositionSide
    quantity: Decimal
    owner_strategy_id: str | None = None
    owner_run_id: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("account position symbol must be uppercase")
        if not self.quantity.is_finite() or self.quantity < 0:
            raise ValueError("account position quantity cannot be negative")
        if (self.owner_strategy_id is None) != (self.owner_run_id is None):
            raise ValueError("account position ownership must be complete or absent")
        if self.owner_strategy_id is not None and (
            not self.owner_strategy_id or not self.owner_run_id
        ):
            raise ValueError("account position ownership cannot be empty")


@dataclass(frozen=True)
class AccountOrder:
    symbol: str
    reduce_only: bool
    owner_strategy_id: str | None = None
    owner_run_id: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("account order symbol must be uppercase")
        if (self.owner_strategy_id is None) != (self.owner_run_id is None):
            raise ValueError("account order ownership must be complete or absent")
        if not isinstance(self.reduce_only, bool):
            raise ValueError("account order reduce_only must be boolean")
        if self.owner_strategy_id is not None and (
            not self.owner_strategy_id or not self.owner_run_id
        ):
            raise ValueError("account order ownership cannot be empty")


@dataclass(frozen=True)
class AccountSnapshot:
    observed_at: datetime
    available_balance: Decimal
    reconciliation_ok: bool
    positions: tuple[AccountPosition, ...] = ()
    open_orders: tuple[AccountOrder, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.reconciliation_ok, bool):
            raise ValueError("account reconciliation flag must be boolean")
        if (
            self.observed_at.tzinfo is None
            or not self.available_balance.is_finite()
            or self.available_balance < 0
        ):
            raise ValueError("account snapshot time or balance is invalid")
        object.__setattr__(self, "positions", tuple(self.positions))
        object.__setattr__(self, "open_orders", tuple(self.open_orders))


@dataclass(frozen=True)
class PortfolioDecision:
    allowed: bool
    strategy_id: str
    run_id: str
    symbol: str
    reason_codes: tuple[str, ...]
    decided_at: datetime

    def __post_init__(self) -> None:
        if self.allowed != (self.reason_codes == ("ALLOWED",)):
            raise ValueError("portfolio decision result and reasons disagree")
        if self.decided_at.tzinfo is None:
            raise ValueError("portfolio decision time must be timezone-aware")


@dataclass(frozen=True)
class PlatformRunStatus:
    strategy_id: str
    run_id: str
    state: PlatformRunState
    capital_budget: Decimal
    reserved_margin: Decimal
    started_at: datetime | None
    updated_at: datetime
    blocking_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.updated_at.tzinfo is None or (
            self.started_at is not None and self.started_at.tzinfo is None
        ):
            raise ValueError("run status timestamps must be timezone-aware")
        if (
            self.capital_budget <= 0
            or not Decimal("0") <= self.reserved_margin <= self.capital_budget
        ):
            raise ValueError("run status capital values are inconsistent")
        if (
            self.state
            in {
                PlatformRunState.BLOCKED,
                PlatformRunState.BLOCKED_STOP_REQUESTED,
            }
            and not self.blocking_reasons
        ):
            raise ValueError("blocked run status requires reasons")


@dataclass(frozen=True)
class ExecutionOutcome:
    run_id: str
    symbol: str
    quantity: Decimal
    expected_price: Decimal
    average_price: Decimal
    realized_pnl: Decimal
    commission: Decimal
    funding: Decimal
    closed_at: datetime
    anomalies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = (
            self.quantity,
            self.expected_price,
            self.average_price,
            self.realized_pnl,
            self.commission,
            self.funding,
        )
        if not all(value.is_finite() for value in values):
            raise ValueError("execution outcome amounts must be finite")
        if self.quantity <= 0 or self.expected_price <= 0 or self.average_price <= 0:
            raise ValueError("execution quantity and prices must be positive")
        if self.commission < 0 or self.closed_at.tzinfo is None:
            raise ValueError("execution commission or close time is invalid")

    @property
    def slippage_bps(self) -> Decimal:
        return (
            abs(self.average_price - self.expected_price)
            / self.expected_price
            * Decimal("10000")
        )


@dataclass(frozen=True)
class PlatformRunReport:
    strategy_id: str
    run_id: str
    generated_at: datetime
    settled_trades: int
    realized_pnl: Decimal
    commission: Decimal
    funding: Decimal
    maximum_slippage_bps: Decimal
    anomalies: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.generated_at.tzinfo is None or self.settled_trades < 0:
            raise ValueError("run report time or trade count is invalid")
        values = (
            self.realized_pnl,
            self.commission,
            self.funding,
            self.maximum_slippage_bps,
        )
        if not all(value.is_finite() for value in values):
            raise ValueError("run report amounts must be finite")
