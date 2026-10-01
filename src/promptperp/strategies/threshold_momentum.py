from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256

from promptperp.domain import OrderSide, PositionSide


@dataclass(frozen=True)
class PriceSnapshot:
    observed_at: datetime
    symbol: str
    price: Decimal

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("price snapshot time must be timezone-aware")
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("price snapshot symbol must be uppercase")
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("price snapshot price must be finite and positive")


@dataclass(frozen=True)
class ThresholdMomentumParameters:
    schema_version: int = 1
    strategy_id: str = "threshold_momentum"
    symbol: str = "BTCUSDT"
    threshold_bps: Decimal = Decimal("25")
    margin: Decimal = Decimal("100")
    leverage: int = 2
    take_profit_ratio: Decimal = Decimal("0.01")
    stop_loss_ratio: Decimal = Decimal("0.005")

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.strategy_id != "threshold_momentum":
            raise ValueError("unsupported momentum parameter schema")
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("momentum symbol must be uppercase")
        decimals = (
            self.threshold_bps,
            self.margin,
            self.take_profit_ratio,
            self.stop_loss_ratio,
        )
        if not all(value.is_finite() and value > 0 for value in decimals):
            raise ValueError("momentum decimal parameters must be finite and positive")
        if self.leverage <= 0:
            raise ValueError("momentum leverage must be positive")
        if self.take_profit_ratio >= 1 or self.stop_loss_ratio >= 1:
            raise ValueError("momentum protection ratios must be below one")

    @property
    def fingerprint(self) -> str:
        values = asdict(self)
        for key, value in tuple(values.items()):
            if isinstance(value, Decimal):
                values[key] = str(value)
        return sha256(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class MomentumSignal:
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


class ThresholdMomentumStrategy:
    """Pure one-step price momentum strategy used as a second runtime plugin."""

    def __init__(self, parameters: ThresholdMomentumParameters):
        self.parameters = parameters
        self._previous_price: Decimal | None = None

    @property
    def previous_price(self) -> Decimal | None:
        return self._previous_price

    def on_price(self, snapshot: PriceSnapshot) -> MomentumSignal | None:
        if snapshot.symbol != self.parameters.symbol:
            raise ValueError("price snapshot does not match momentum symbol")
        previous = self._previous_price
        self._previous_price = snapshot.price
        if previous is None:
            return None
        change_bps = (snapshot.price - previous) / previous * Decimal("10000")
        if abs(change_bps) < self.parameters.threshold_bps:
            return None
        is_long = change_bps > 0
        return MomentumSignal(
            strategy_id=self.parameters.strategy_id,
            symbol=snapshot.symbol,
            side=OrderSide.BUY if is_long else OrderSide.SELL,
            position_side=PositionSide.LONG if is_long else PositionSide.SHORT,
            margin=self.parameters.margin,
            leverage=self.parameters.leverage,
            take_profit_ratio=self.parameters.take_profit_ratio,
            stop_loss_ratio=self.parameters.stop_loss_ratio,
            observed_at=snapshot.observed_at,
            reason=f"one_step_momentum_{'up' if is_long else 'down'}",
        )
