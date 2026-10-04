from __future__ import annotations

from typing import Protocol

from promptperp.domain import (
    Order,
    OrderSide,
    OrderStatus,
    PositionSide,
    ReconciliationFailed,
    RequestRejected,
    RequestUnknown,
    RiskRejected,
)
from promptperp.exchange import ExecutionGateway
from promptperp.execution.basket import BasketAdjustmentRole
from promptperp.execution.basket_state import (
    BasketExecutionLeg,
    BasketExecutionSnapshot,
    BasketExecutionStateMachine,
    BasketLegState,
    BasketPhase,
)
from promptperp.execution.virtual_routes import VirtualFillRole


class BasketAuthorization(Protocol):
    @property
    def allowed(self) -> bool: ...

    @property
    def strategy_id(self) -> str: ...

    @property
    def run_id(self) -> str: ...

    @property
    def basket_id(self) -> str: ...

    @property
    def target_fingerprint(self) -> str: ...


class BasketFillRouteSink(Protocol):
    def prepare(
        self,
        *,
        strategy_id: str,
        run_id: str,
        symbol: str,
        position_side: PositionSide,
        client_order_id: str,
        role: VirtualFillRole,
    ) -> None: ...

    def confirm(self, *, client_order_id: str, exchange_order_id: str) -> None: ...


class BasketExecutionCoordinator:
    """Execute one authorized basket leg at a time and recover by client ID."""

    def __init__(
        self,
        *,
        state_machine: BasketExecutionStateMachine,
        execution_gateway: ExecutionGateway,
        fill_route_sink: BasketFillRouteSink,
    ):
        self.state_machine = state_machine
        self.execution_gateway = execution_gateway
        self.fill_route_sink = fill_route_sink

    def execute_next(
        self, authorization: BasketAuthorization
    ) -> BasketExecutionSnapshot:
        snapshot = self._authorized(authorization)
        if snapshot.phase is BasketPhase.PREPARED:
            snapshot = self.state_machine.start()
        snapshot = self.state_machine.resume()
        if snapshot.phase in {
            BasketPhase.COMPLETE,
            BasketPhase.SAFE_FLAT,
            BasketPhase.BLOCKED,
        }:
            return snapshot
        if snapshot.phase is BasketPhase.ROLLING_BACK:
            raise ReconciliationFailed(
                "basket requires strategy-owned flatten reconciliation"
            )
        leg = self.state_machine.next_leg()
        if leg is None:
            raise ReconciliationFailed("basket phase has no recoverable next leg")
        if leg.state is BasketLegState.PENDING:
            self.fill_route_sink.prepare(
                strategy_id=snapshot.strategy_id,
                run_id=snapshot.run_id,
                symbol=leg.adjustment.symbol,
                position_side=leg.adjustment.position_side,
                client_order_id=leg.client_order_id,
                role=(
                    VirtualFillRole.OPEN
                    if leg.adjustment.role is BasketAdjustmentRole.INCREASE
                    else VirtualFillRole.REDUCE
                ),
            )
            self.state_machine.mark_submitted(leg.index)
            leg = self._leg(leg.index)
            try:
                order = self.execution_gateway.place_market_order(
                    symbol=leg.adjustment.symbol,
                    side=self._order_side(leg),
                    position_side=leg.adjustment.position_side,
                    quantity=leg.adjustment.quantity,
                    client_order_id=leg.client_order_id,
                )
            except RequestUnknown:
                raise
            except RequestRejected:
                return self.state_machine.fail_leg(
                    leg.index, reason="exchange definitively rejected the leg"
                )
        else:
            try:
                order = self.execution_gateway.get_order(
                    symbol=leg.adjustment.symbol,
                    client_order_id=leg.client_order_id,
                )
            except RequestUnknown as exc:
                raise ReconciliationFailed(
                    "basket order remains unknown by deterministic client ID"
                ) from exc
        return self._apply_order(leg, order)

    def _apply_order(
        self, leg: BasketExecutionLeg, order: Order
    ) -> BasketExecutionSnapshot:
        if (
            order.symbol != leg.adjustment.symbol
            or order.client_order_id != leg.client_order_id
            or order.side is not self._order_side(leg)
            or order.position_side is not leg.adjustment.position_side
            or order.requested_quantity != leg.adjustment.quantity
        ):
            raise ReconciliationFailed("basket order does not match its owned leg")
        if order.status is OrderStatus.UNKNOWN:
            raise ReconciliationFailed("basket order status remains unknown")
        self.fill_route_sink.confirm(
            client_order_id=leg.client_order_id,
            exchange_order_id=order.order_id,
        )
        self.state_machine.confirm(
            leg.index,
            exchange_order_id=order.order_id,
            cumulative_filled=order.executed_quantity,
            average_price=order.average_price,
        )
        if order.status in {
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
        }:
            current = self._leg(leg.index)
            if current.state is BasketLegState.FILLED:
                return self._snapshot()
            return self.state_machine.fail_leg(
                leg.index,
                reason=f"exchange order ended as {order.status.value}",
            )
        if order.status not in {
            OrderStatus.NEW,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
        }:
            raise ReconciliationFailed("basket order status is unsupported")
        return self._snapshot()

    def reconcile_pending(self) -> BasketExecutionSnapshot:
        """Query existing client IDs only; never submit a pending new leg."""
        snapshot = self._snapshot()
        for leg in snapshot.legs:
            if leg.state not in {
                BasketLegState.SUBMITTED,
                BasketLegState.PARTIALLY_FILLED,
            }:
                continue
            order = self.execution_gateway.get_order(
                symbol=leg.adjustment.symbol,
                client_order_id=leg.client_order_id,
            )
            self._apply_order(leg, order)
        return self._snapshot()

    def cancel_pending(
        self, authorization: BasketAuthorization
    ) -> BasketExecutionSnapshot:
        snapshot = self._authorized(authorization)
        for leg in snapshot.legs:
            if leg.state not in {
                BasketLegState.SUBMITTED,
                BasketLegState.PARTIALLY_FILLED,
            }:
                continue
            order = self.execution_gateway.get_order(
                symbol=leg.adjustment.symbol,
                client_order_id=leg.client_order_id,
            )
            self._apply_order(leg, order)
            if order.status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}:
                self.execution_gateway.cancel_order(
                    symbol=leg.adjustment.symbol,
                    order_id=order.order_id,
                )
            # A cancellation acknowledgement is not proof of the final fills.
            order = self.execution_gateway.get_order(
                symbol=leg.adjustment.symbol,
                client_order_id=leg.client_order_id,
            )
            if order.status not in {
                OrderStatus.FILLED,
                OrderStatus.CANCELED,
                OrderStatus.EXPIRED,
                OrderStatus.REJECTED,
            }:
                raise ReconciliationFailed("basket cancellation is not terminal")
            self._apply_order(leg, order)
        return self._snapshot()

    def _authorized(
        self, authorization: BasketAuthorization
    ) -> BasketExecutionSnapshot:
        snapshot = self._snapshot()
        identity = (
            snapshot.strategy_id,
            snapshot.run_id,
            snapshot.basket_id,
            snapshot.target_fingerprint,
        )
        authorized = (
            authorization.strategy_id,
            authorization.run_id,
            authorization.basket_id,
            authorization.target_fingerprint,
        )
        if not authorization.allowed or identity != authorized:
            raise RiskRejected("basket execution authorization does not bind")
        return snapshot

    def _snapshot(self) -> BasketExecutionSnapshot:
        snapshot = self.state_machine.snapshot
        if snapshot is None:
            raise ReconciliationFailed("basket execution state is missing")
        return snapshot

    def _leg(self, index: int) -> BasketExecutionLeg:
        snapshot = self._snapshot()
        try:
            leg = snapshot.legs[index]
        except IndexError as exc:
            raise ReconciliationFailed("basket leg state is missing") from exc
        if leg.index != index:
            raise ReconciliationFailed("basket leg identity changed")
        return leg

    @staticmethod
    def _order_side(leg: BasketExecutionLeg) -> OrderSide:
        opens_long = leg.adjustment.position_side.value == "LONG"
        increases = leg.adjustment.role is BasketAdjustmentRole.INCREASE
        return OrderSide.BUY if opens_long == increases else OrderSide.SELL
