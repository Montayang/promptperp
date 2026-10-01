from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from promptperp.domain import PositionSide


@dataclass(frozen=True)
class RankingSnapshot:
    observed_at: datetime
    symbols: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("ranking snapshot time must be timezone-aware")
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("ranking symbols must be non-empty and unique")
        if any(not symbol or symbol != symbol.upper() for symbol in self.symbols):
            raise ValueError("ranking symbols must be uppercase")


@dataclass(frozen=True)
class StrategySignal:
    strategy_id: str
    symbol: str
    position_side: PositionSide
    margin: Decimal
    leverage: int
    take_profit_ratio: Decimal
    stop_loss_ratio: Decimal
    observed_at: datetime
    trace: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.symbol:
            raise ValueError("signal identity is required")
        if self.margin <= 0 or self.leverage <= 0:
            raise ValueError("signal exposure must be positive")
        if not Decimal("0") < self.take_profit_ratio < Decimal("1"):
            raise ValueError("take-profit ratio must be between zero and one")
        if not Decimal("0") < self.stop_loss_ratio < Decimal("1"):
            raise ValueError("stop-loss ratio must be between zero and one")
