from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class AllocationTarget:
    strategy_id: str
    fraction: Decimal
    max_margin_per_trade: Decimal
    max_leverage: int
    max_loss_per_trade: Decimal
    max_positions: int

    def __post_init__(self) -> None:
        if not self.strategy_id:
            raise ValueError("allocation strategy identity is required")
        if not self.fraction.is_finite() or not Decimal("0") < self.fraction <= 1:
            raise ValueError("allocation fraction must be in (0, 1]")
        if not self.max_margin_per_trade.is_finite() or self.max_margin_per_trade <= 0:
            raise ValueError("allocation per-trade margin must be positive")
        if self.max_leverage <= 0 or self.max_positions <= 0:
            raise ValueError("strategy leverage and position limits must be positive")
        if not self.max_loss_per_trade.is_finite() or self.max_loss_per_trade <= 0:
            raise ValueError("strategy loss limit must be positive")


@dataclass(frozen=True)
class CapitalAllocationPlan:
    allocatable_equity: Decimal
    reserve_fraction: Decimal
    targets: tuple[AllocationTarget, ...]

    def __post_init__(self) -> None:
        if not self.allocatable_equity.is_finite() or self.allocatable_equity <= 0:
            raise ValueError("allocatable equity must be positive")
        if (
            not self.reserve_fraction.is_finite()
            or not Decimal("0") <= self.reserve_fraction < 1
        ):
            raise ValueError("reserve fraction must be in [0, 1)")
        if not self.targets:
            raise ValueError("at least one allocation target is required")
        identities = [target.strategy_id for target in self.targets]
        if len(set(identities)) != len(identities):
            raise ValueError("allocation strategy identities must be unique")
        if sum((target.fraction for target in self.targets), Decimal("0")) > (
            Decimal("1") - self.reserve_fraction
        ):
            raise ValueError("strategy allocations exceed equity after reserve")

    @property
    def budgets(self) -> Mapping[str, Decimal]:
        return MappingProxyType(
            {
                target.strategy_id: self.allocatable_equity * target.fraction
                for target in self.targets
            }
        )

    def target(self, strategy_id: str) -> AllocationTarget:
        for target in self.targets:
            if target.strategy_id == strategy_id:
                return target
        raise ValueError("strategy has no capital allocation")

    def budget(self, strategy_id: str) -> Decimal:
        target = self.target(strategy_id)
        return self.allocatable_equity * target.fraction
