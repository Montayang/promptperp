from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from promptperp.domain import (
    Fill,
    FuturesTradeFill,
    OrderSide,
    PositionSide,
    ReconciliationFailed,
)
from promptperp.execution.virtual_positions import (
    VirtualPosition,
    VirtualPositionStore,
)
from promptperp.execution.virtual_routes import VirtualFillRole, VirtualFillRoute


class VirtualFillAttributor:
    """Apply an owned exchange fill to exactly one strategy virtual lot."""

    def __init__(self, store: VirtualPositionStore):
        self.store = store

    def apply(
        self,
        *,
        fill: Fill,
        route: VirtualFillRoute,
        realized_pnl: Decimal,
        occurred_at: datetime,
    ) -> VirtualPosition:
        self.validate(
            fill=fill,
            route=route,
            realized_pnl=realized_pnl,
            occurred_at=occurred_at,
        )
        event_id = f"binance:fill:{fill.symbol}:{fill.trade_id}"
        if route.role is VirtualFillRole.OPEN:
            return self.store.open_fill(
                event_id=event_id,
                strategy_id=route.strategy_id,
                run_id=route.run_id,
                symbol=route.symbol,
                position_side=route.position_side,
                quantity=fill.quantity,
                price=fill.price,
                commission=fill.commission,
                occurred_at=occurred_at,
            )
        return self.store.close_fill(
            event_id=event_id,
            strategy_id=route.strategy_id,
            run_id=route.run_id,
            symbol=route.symbol,
            position_side=route.position_side,
            quantity=fill.quantity,
            price=fill.price,
            commission=fill.commission,
            occurred_at=occurred_at,
            realized_pnl=realized_pnl,
        )

    def validate(
        self,
        *,
        fill: Fill,
        route: VirtualFillRoute,
        realized_pnl: Decimal,
        occurred_at: datetime,
    ) -> None:
        if not realized_pnl.is_finite() or occurred_at.tzinfo is None:
            raise ValueError("fill realized PnL and time are invalid")
        if (
            fill.symbol != route.symbol
            or fill.order_id != route.exchange_order_id
            or fill.position_side is not route.position_side
            or fill.side is not self._expected_side(route)
        ):
            raise ReconciliationFailed("exchange fill does not match virtual ownership")
        if fill.commission_asset != "USDT":
            raise ReconciliationFailed("virtual attribution requires USDT commission")
        if route.role is VirtualFillRole.OPEN:
            if realized_pnl != 0:
                raise ReconciliationFailed("opening fill unexpectedly realized PnL")

    def apply_trade(
        self, *, trade: FuturesTradeFill, route: VirtualFillRoute
    ) -> VirtualPosition:
        return self.apply(
            fill=trade.fill,
            route=route,
            realized_pnl=trade.realized_pnl,
            occurred_at=trade.occurred_at,
        )

    @staticmethod
    def _expected_side(route: VirtualFillRoute) -> OrderSide:
        opens_long = route.position_side is PositionSide.LONG
        opens = route.role is VirtualFillRole.OPEN
        return OrderSide.BUY if opens_long == opens else OrderSide.SELL
