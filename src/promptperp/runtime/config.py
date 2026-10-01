from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from promptperp.runtime.allocation import AllocationTarget, CapitalAllocationPlan
from promptperp.runtime.portfolio import PortfolioPolicy

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore[import-not-found]


@dataclass(frozen=True)
class PlatformConfiguration:
    allocation: CapitalAllocationPlan
    portfolio: PortfolioPolicy


def load_platform_configuration(path: str | Path) -> PlatformConfiguration:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    portfolio = raw.get("portfolio")
    allocations = raw.get("allocation")
    if not isinstance(portfolio, dict) or not isinstance(allocations, list):
        raise ValueError("platform config needs [portfolio] and [[allocation]]")
    allocation = CapitalAllocationPlan(
        allocatable_equity=Decimal(str(portfolio["allocatable_equity"])),
        reserve_fraction=Decimal(str(portfolio.get("reserve_fraction", "0"))),
        targets=tuple(
            AllocationTarget(
                strategy_id=str(item["strategy_id"]),
                fraction=Decimal(str(item["fraction"])),
                max_margin_per_trade=Decimal(str(item["max_margin_per_trade"])),
                max_leverage=int(item["max_leverage"]),
                max_loss_per_trade=Decimal(str(item["max_loss_per_trade"])),
                max_positions=int(item.get("max_positions", 1)),
            )
            for item in allocations
        ),
    )
    policy = PortfolioPolicy(
        allowed_symbols=frozenset(str(item) for item in portfolio["allowed_symbols"]),
        max_total_positions=int(portfolio["max_total_positions"]),
        max_total_margin=Decimal(str(portfolio["max_total_margin"])),
        balance_buffer=Decimal(str(portfolio.get("balance_buffer", "0"))),
        max_signal_age_seconds=Decimal(
            str(portfolio.get("max_signal_age_seconds", "30"))
        ),
        max_leverage=int(portfolio["max_leverage"]),
        max_account_age_seconds=Decimal(
            str(portfolio.get("max_account_age_seconds", "15"))
        ),
        shared_symbol_policy=str(portfolio.get("shared_symbol_policy", "EXCLUSIVE")),
    )
    return PlatformConfiguration(allocation=allocation, portfolio=policy)
