from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from promptperp.domain import (
    ForeignOrderDetected,
    ForeignPositionDetected,
    FuturesPosition,
    Order,
    PositionSide,
    ReconciliationFailed,
)
from promptperp.execution.models import ExecutionSnapshot


class OwnershipReconciler:
    @staticmethod
    def ensure_run_can_open(snapshots: Iterable[ExecutionSnapshot]) -> None:
        for snapshot in snapshots:
            if snapshot.state.value != "SETTLED":
                raise ReconciliationFailed(
                    "run has unresolved execution state; new entries are blocked"
                )

    @staticmethod
    def verify_orders(
        snapshots: Iterable[ExecutionSnapshot],
        open_orders: Iterable[Order],
    ) -> None:
        owned_ids = {
            value
            for snapshot in snapshots
            for value in (
                snapshot.entry_client_order_id,
                snapshot.stop_client_order_id,
                snapshot.take_profit_client_order_id,
            )
            if value
        }
        for order in open_orders:
            if not order.client_order_id or order.client_order_id not in owned_ids:
                raise ForeignOrderDetected("foreign open order detected")

    @staticmethod
    def verify_positions(
        snapshots: Iterable[ExecutionSnapshot],
        positions: Iterable[FuturesPosition],
    ) -> None:
        owned: dict[tuple[str, PositionSide], Decimal] = {}
        for snapshot in snapshots:
            if not snapshot.owns_position:
                continue
            key = (snapshot.intent.symbol, snapshot.intent.position_side)
            owned[key] = owned.get(key, 0) + snapshot.owned_quantity
        for position in positions:
            quantity = owned.get((position.symbol, position.side))
            if quantity is None or position.quantity != quantity:
                raise ForeignPositionDetected("foreign or mismatched position detected")
