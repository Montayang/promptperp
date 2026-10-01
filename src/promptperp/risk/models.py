from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from promptperp.execution import TradeIntent


class RiskAction(str, Enum):
    OPEN = "OPEN"
    REDUCE = "REDUCE"


class OperatingMode(str, Enum):
    NORMAL = "NORMAL"
    REDUCE_ONLY = "REDUCE_ONLY"
    KILL_SWITCH = "KILL_SWITCH"


@dataclass(frozen=True)
class RiskPolicy:
    policy_id: str
    version: int
    mode: OperatingMode
    allowed_markets: frozenset[str]
    allowed_symbols: frozenset[str]
    allowed_sides: frozenset[str]
    allowed_order_types: frozenset[str]
    max_margin: Decimal
    max_notional: Decimal
    max_leverage: int
    max_open_positions: int
    balance_buffer: Decimal
    require_stop_loss: bool
    max_loss_per_trade: Decimal
    max_market_age_seconds: Decimal
    max_price_deviation_bps: Decimal
    max_orders_per_window: int
    max_consecutive_failures: int
    daily_loss_limit: Decimal

    def __post_init__(self) -> None:
        if not self.policy_id or self.version <= 0:
            raise ValueError("risk policy identity and version are required")
        if not isinstance(self.mode, OperatingMode):
            raise ValueError("risk policy mode must be an OperatingMode")
        if not self.allowed_markets or not self.allowed_symbols:
            raise ValueError("risk policy allowlists cannot be empty")
        if not self.allowed_sides <= {"BUY", "SELL"}:
            raise ValueError("risk policy sides must be BUY or SELL")
        if not self.allowed_order_types:
            raise ValueError("risk policy order types cannot be empty")
        decimal_limits = (
            self.max_margin,
            self.max_notional,
            self.balance_buffer,
            self.max_loss_per_trade,
            self.max_market_age_seconds,
            self.max_price_deviation_bps,
            self.daily_loss_limit,
        )
        if not all(value.is_finite() and value >= 0 for value in decimal_limits):
            raise ValueError(
                "risk policy decimal limits must be finite and non-negative"
            )
        if (
            self.max_margin <= 0
            or self.max_notional <= 0
            or self.max_leverage <= 0
            or self.max_open_positions <= 0
            or self.max_orders_per_window <= 0
            or self.max_consecutive_failures <= 0
            or self.daily_loss_limit <= 0
        ):
            raise ValueError("risk policy limits must be positive")
        object.__setattr__(
            self, "allowed_markets", frozenset(x.upper() for x in self.allowed_markets)
        )
        object.__setattr__(
            self, "allowed_symbols", frozenset(x.upper() for x in self.allowed_symbols)
        )
        object.__setattr__(
            self, "allowed_sides", frozenset(x.upper() for x in self.allowed_sides)
        )
        object.__setattr__(
            self,
            "allowed_order_types",
            frozenset(x.upper() for x in self.allowed_order_types),
        )

    @property
    def fingerprint(self) -> str:
        values = asdict(self)
        values["mode"] = self.mode.value
        for key in (
            "allowed_markets",
            "allowed_symbols",
            "allowed_sides",
            "allowed_order_types",
        ):
            values[key] = sorted(values[key])
        for key, value in tuple(values.items()):
            if isinstance(value, Decimal):
                values[key] = str(value)
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RiskRequest:
    intent: TradeIntent
    action: RiskAction
    market: str
    order_type: str
    margin: Decimal
    notional: Decimal
    leverage: int
    available_balance: Decimal
    reference_price: Decimal
    expected_price: Decimal
    market_time: datetime
    evaluated_at: datetime
    stop_loss_price: Decimal | None
    open_position_count: int
    owned_position_quantity: Decimal = Decimal("0")
    recent_order_count: int = 0
    consecutive_failures: int = 0
    daily_pnl: Decimal = Decimal("0")
    ownership_proven: bool = False
    order_outcome_unknown: bool = False
    reconciliation_failed: bool = False
    protection_incomplete: bool = False
    foreign_objects_detected: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.action, RiskAction):
            raise ValueError("risk request action must be a RiskAction")
        amounts = [
            self.margin,
            self.notional,
            self.available_balance,
            self.reference_price,
            self.expected_price,
            self.owned_position_quantity,
            self.daily_pnl,
        ]
        if self.stop_loss_price is not None:
            amounts.append(self.stop_loss_price)
        if not all(value.is_finite() for value in amounts):
            raise ValueError("risk request amounts must be finite")
        if self.action is RiskAction.OPEN and self.margin <= 0:
            raise ValueError("opening margin must be positive")
        if self.margin < 0 or self.notional <= 0 or self.leverage <= 0:
            raise ValueError("risk request exposure must be positive")
        if self.reference_price <= 0 or self.expected_price <= 0:
            raise ValueError("risk request prices must be positive")
        if self.available_balance < 0 or self.owned_position_quantity < 0:
            raise ValueError(
                "risk request balances and owned quantity cannot be negative"
            )
        if (
            min(
                self.open_position_count,
                self.recent_order_count,
                self.consecutive_failures,
            )
            < 0
        ):
            raise ValueError("risk request counters cannot be negative")
        if self.market_time.tzinfo is None or self.evaluated_at.tzinfo is None:
            raise ValueError("risk request timestamps must be timezone-aware")


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    policy_id: str
    policy_version: int
    policy_fingerprint: str
    strategy_id: str
    run_id: str
    intent_id: str
    action: RiskAction
    reason_codes: tuple[str, ...]
    decided_at: datetime
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(
            (
                self.policy_id,
                self.policy_fingerprint,
                self.strategy_id,
                self.run_id,
                self.intent_id,
                self.reason_codes,
            )
        ):
            raise ValueError("risk decision identity and reasons are required")
        if self.policy_version <= 0 or self.decided_at.tzinfo is None:
            raise ValueError("risk decision version and timestamp are invalid")
        if self.allowed != (self.reason_codes == ("ALLOWED",)):
            raise ValueError("risk decision allowed flag and reasons disagree")
        object.__setattr__(self, "context", MappingProxyType(dict(self.context)))


class RiskAuditSink(Protocol):
    def record(self, decision: RiskDecision) -> None: ...


class MemoryRiskAuditSink:
    def __init__(self) -> None:
        self.decisions: list[RiskDecision] = []

    def record(self, decision: RiskDecision) -> None:
        self.decisions.append(decision)
