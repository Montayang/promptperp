from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from promptperp.domain import (
    Order,
    OrderSide,
    OrderStatus,
    ProtectionClose,
    ProtectionFailed,
    ReconciliationFailed,
    RequestUnknown,
    RiskRejected,
)
from promptperp.exchange import ExecutionGateway
from promptperp.execution.ids import client_order_id
from promptperp.execution.models import (
    ExecutionSnapshot,
    ExecutionState,
    TradeIntent,
)
from promptperp.execution.ownership_sink import ExecutionOwnershipSink
from promptperp.execution.state_machine import ExecutionStateMachine


class RiskAuthorization(Protocol):
    @property
    def allowed(self) -> bool: ...

    @property
    def strategy_id(self) -> str: ...

    @property
    def run_id(self) -> str: ...

    @property
    def intent_id(self) -> str: ...

    @property
    def action(self) -> object: ...

    @property
    def policy_fingerprint(self) -> str: ...


class ProtectionGateway(Protocol):
    def place_protection(
        self,
        *,
        snapshot: ExecutionSnapshot,
        stop_client_order_id: str,
        take_profit_client_order_id: str,
    ) -> None: ...

    def get_protection_order_id(
        self,
        *,
        snapshot: ExecutionSnapshot,
        client_order_id: str,
        role: str,
    ) -> str | None: ...

    def get_triggered_close(
        self,
        *,
        snapshot: ExecutionSnapshot,
    ) -> ProtectionClose | None: ...

    def cancel_open_protection(self, *, snapshot: ExecutionSnapshot) -> None: ...


class ExecutionCoordinator:
    def __init__(
        self,
        *,
        state_machine: ExecutionStateMachine,
        execution_gateway: ExecutionGateway,
        ownership_sink: ExecutionOwnershipSink | None = None,
    ):
        self.state_machine = state_machine
        self.execution_gateway = execution_gateway
        self.ownership_sink = ownership_sink

    def submit_entry(
        self,
        intent: TradeIntent,
        authorization: RiskAuthorization,
    ) -> ExecutionSnapshot:
        self._require_open_authorization(intent, authorization)
        snapshot = self.state_machine.create(intent)
        if self.ownership_sink is not None:
            opened_at = self._last_event_at()
            self.ownership_sink.begin_intent(intent, opened_at=opened_at)
            self.ownership_sink.prepare_order(
                intent_id=intent.intent_id,
                role="ENTRY",
                client_order_id=snapshot.entry_client_order_id,
                is_algo=False,
                created_at=opened_at,
            )
        snapshot = self.state_machine.transition(
            ExecutionState.SUBMITTED,
            reason="entry submission started",
        )
        try:
            order = self.execution_gateway.place_market_order(
                symbol=intent.symbol,
                side=intent.side,
                position_side=intent.position_side,
                quantity=intent.quantity,
                client_order_id=snapshot.entry_client_order_id,
            )
        except RequestUnknown:
            raise
        self._confirm_owned_order(
            snapshot.entry_client_order_id,
            order.order_id,
        )
        return self._apply_entry_order(order)

    def submit_close(
        self,
        authorization: RiskAuthorization,
        *,
        reason: str,
    ) -> ExecutionSnapshot:
        """Submit one deterministic reduce order for the owned position.

        The state is persisted as CLOSING before the exchange write.  An
        uncertain response must therefore be recovered by client order ID and
        can never cause a second close submission.
        """

        snapshot = self._snapshot()
        self._require_authorization(
            snapshot.intent,
            authorization,
            expected_action="REDUCE",
        )
        if snapshot.state not in {
            ExecutionState.FILLED,
            ExecutionState.PROTECTING,
            ExecutionState.PROTECTED,
        }:
            raise ReconciliationFailed("position is not eligible for close submission")
        if not reason:
            raise ValueError("close reason is required")
        close_id = client_order_id(
            strategy_id=snapshot.intent.strategy_id,
            run_id=snapshot.intent.run_id,
            intent_id=snapshot.intent.intent_id,
            role="exit",
        )
        snapshot = self.state_machine.transition(
            ExecutionState.CLOSING,
            reason=reason,
            updates={"close_client_order_id": close_id},
        )
        if self.ownership_sink is not None:
            self.ownership_sink.prepare_order(
                intent_id=snapshot.intent.intent_id,
                role="EXIT",
                client_order_id=close_id,
                is_algo=False,
                created_at=self._last_event_at(),
            )
        try:
            order = self.execution_gateway.place_market_order(
                symbol=snapshot.intent.symbol,
                side=self._opposite_side(snapshot.intent.side),
                position_side=snapshot.intent.position_side,
                quantity=snapshot.filled_quantity,
                client_order_id=close_id,
            )
        except RequestUnknown:
            raise
        self._confirm_owned_order(close_id, order.order_id)
        return self._apply_close_order(order)

    def recover_close(self) -> ExecutionSnapshot:
        snapshot = self._snapshot()
        if snapshot.state is not ExecutionState.CLOSING:
            raise ReconciliationFailed("close is not awaiting reconciliation")
        if not snapshot.close_client_order_id:
            raise ReconciliationFailed("close client order identity is missing")
        try:
            order = self.execution_gateway.get_order(
                symbol=snapshot.intent.symbol,
                client_order_id=snapshot.close_client_order_id,
            )
        except RequestUnknown as exc:
            raise ReconciliationFailed("close order remains unknown") from exc
        self._confirm_owned_order(snapshot.close_client_order_id, order.order_id)
        return self._apply_close_order(order)

    def recover_protection_close(
        self,
        gateway: ProtectionGateway,
    ) -> ExecutionSnapshot:
        """Confirm a stop/take-profit trigger as the owned complete close."""

        snapshot = self._snapshot()
        if snapshot.state is not ExecutionState.PROTECTED:
            raise ReconciliationFailed("execution is not awaiting a protection close")
        try:
            triggered = gateway.get_triggered_close(snapshot=snapshot)
        except Exception as exc:
            raise ReconciliationFailed("protection close query failed") from exc
        if triggered is None:
            raise ReconciliationFailed("no owned protection close was confirmed")
        if triggered.protection_client_order_id not in {
            snapshot.stop_client_order_id,
            snapshot.take_profit_client_order_id,
        }:
            raise ReconciliationFailed("triggered protection identity is not owned")
        order = triggered.order
        if (
            order.symbol != snapshot.intent.symbol
            or order.side is not self._opposite_side(snapshot.intent.side)
            or order.position_side is not snapshot.intent.position_side
            or order.requested_quantity != snapshot.filled_quantity
            or order.status is not OrderStatus.FILLED
            or order.executed_quantity != snapshot.filled_quantity
            or order.average_price <= 0
        ):
            raise ReconciliationFailed(
                "triggered protection close does not match intent"
            )
        if self.ownership_sink is not None:
            self.ownership_sink.confirm_algo_trigger(
                client_order_id=triggered.protection_client_order_id,
                exchange_order_id=order.order_id,
            )
        self.state_machine.transition(
            ExecutionState.CLOSING,
            reason="owned exchange protection triggered",
            updates={
                "close_client_order_id": triggered.protection_client_order_id,
            },
        )
        return self.state_machine.transition(
            ExecutionState.CLOSED,
            reason="protection close fully filled",
            updates={
                "close_exchange_order_id": order.order_id,
                "close_filled_quantity": order.executed_quantity,
                "close_average_price": order.average_price,
            },
        )

    def settle(
        self,
        *,
        realized_pnl: Decimal,
        commission: Decimal,
        funding: Decimal,
        reason: str = "owned execution settlement confirmed",
    ) -> ExecutionSnapshot:
        """Persist attributed settlement only after a confirmed complete close."""

        snapshot = self._snapshot()
        if snapshot.state is not ExecutionState.CLOSED:
            raise ReconciliationFailed("execution is not ready for settlement")
        if (
            snapshot.stop_client_order_id
            and snapshot.take_profit_client_order_id
            and not snapshot.metadata.get("protection_cleanup_confirmed")
        ):
            raise ReconciliationFailed("execution protection cleanup is not confirmed")
        return self.state_machine.transition(
            ExecutionState.SETTLED,
            reason=reason,
            updates={
                "realized_pnl": realized_pnl,
                "commission": commission,
                "funding": funding,
            },
        )

    def cleanup_protection(
        self,
        gateway: ProtectionGateway,
    ) -> ExecutionSnapshot:
        """Cancel only this intent's remaining protection before settlement."""

        snapshot = self._snapshot()
        if snapshot.state is not ExecutionState.CLOSED:
            raise ReconciliationFailed("execution is not closed for protection cleanup")
        if snapshot.metadata.get("protection_cleanup_confirmed"):
            return snapshot
        try:
            gateway.cancel_open_protection(snapshot=snapshot)
        except Exception as exc:
            raise ReconciliationFailed("owned protection cleanup failed") from exc
        metadata = dict(snapshot.metadata)
        metadata["protection_cleanup_confirmed"] = True
        return self.state_machine.transition(
            ExecutionState.CLOSED,
            reason="owned protection cleanup confirmed",
            updates={"metadata": metadata},
        )

    def closed_at(self) -> datetime:
        """Return the durable timestamp of the first complete-close transition."""

        for event in self.state_machine.ledger.load():
            if (
                event.event_type == "STATE_TRANSITION"
                and event.payload.get("to") == ExecutionState.CLOSED.value
            ):
                return event.occurred_at
        raise ReconciliationFailed("execution has no durable close timestamp")

    def recover_entry(self) -> ExecutionSnapshot:
        snapshot = self._snapshot()
        if snapshot.state not in {
            ExecutionState.SUBMITTED,
            ExecutionState.ACKNOWLEDGED,
            ExecutionState.PARTIALLY_FILLED,
        }:
            raise ReconciliationFailed("entry is not awaiting reconciliation")
        try:
            order = self.execution_gateway.get_order(
                symbol=snapshot.intent.symbol,
                client_order_id=snapshot.entry_client_order_id,
            )
        except RequestUnknown as exc:
            raise ReconciliationFailed("entry order remains unknown") from exc
        self._confirm_owned_order(snapshot.entry_client_order_id, order.order_id)
        return self._apply_entry_order(order)

    def ensure_protection(self, gateway: ProtectionGateway) -> ExecutionSnapshot:
        snapshot = self._snapshot()
        stop_id = client_order_id(
            strategy_id=snapshot.intent.strategy_id,
            run_id=snapshot.intent.run_id,
            intent_id=snapshot.intent.intent_id,
            role="stop",
        )
        take_id = client_order_id(
            strategy_id=snapshot.intent.strategy_id,
            run_id=snapshot.intent.run_id,
            intent_id=snapshot.intent.intent_id,
            role="take",
        )
        if snapshot.state is ExecutionState.FILLED:
            snapshot = self.state_machine.transition(
                ExecutionState.PROTECTING,
                reason="protection submission started",
                updates={
                    "stop_client_order_id": stop_id,
                    "take_profit_client_order_id": take_id,
                },
            )
            self._prepare_protection_ownership(snapshot, stop_id, take_id)
            try:
                gateway.place_protection(
                    snapshot=snapshot,
                    stop_client_order_id=stop_id,
                    take_profit_client_order_id=take_id,
                )
            except Exception as exc:
                raise ProtectionFailed("protection submission is unknown") from exc
        elif snapshot.state is not ExecutionState.PROTECTING:
            raise ProtectionFailed("position is not awaiting protection")
        else:
            self._prepare_protection_ownership(snapshot, stop_id, take_id)
        try:
            stop_exchange_id = gateway.get_protection_order_id(
                snapshot=snapshot,
                client_order_id=stop_id,
                role="STOP",
            )
            take_exchange_id = gateway.get_protection_order_id(
                snapshot=snapshot,
                client_order_id=take_id,
                role="TAKE_PROFIT",
            )
        except Exception as exc:
            raise ProtectionFailed("protection confirmation failed") from exc
        if not stop_exchange_id or not take_exchange_id:
            raise ProtectionFailed("both protection orders were not confirmed")
        self._confirm_owned_order(stop_id, stop_exchange_id)
        self._confirm_owned_order(take_id, take_exchange_id)
        return self.state_machine.transition(
            ExecutionState.PROTECTED,
            reason="both protection orders confirmed by query",
            updates={
                "stop_exchange_order_id": stop_exchange_id,
                "take_profit_exchange_order_id": take_exchange_id,
            },
        )

    def _apply_entry_order(self, order: Order) -> ExecutionSnapshot:
        snapshot = self._snapshot()
        intent = snapshot.intent
        if (
            order.symbol != intent.symbol
            or order.client_order_id != snapshot.entry_client_order_id
            or order.side is not intent.side
            or order.position_side is not intent.position_side
            or order.requested_quantity != intent.quantity
        ):
            raise ReconciliationFailed(
                "exchange order ownership or intent does not match"
            )
        updates = {
            "exchange_order_id": order.order_id,
            "filled_quantity": order.executed_quantity,
            "average_price": order.average_price,
        }
        if snapshot.state is ExecutionState.SUBMITTED:
            snapshot = self.state_machine.transition(
                ExecutionState.ACKNOWLEDGED,
                reason="entry order identity confirmed",
                updates=updates,
            )
        if order.status is OrderStatus.FILLED:
            return self.state_machine.transition(
                ExecutionState.FILLED,
                reason="entry fully filled",
                updates=updates,
            )
        if order.status is OrderStatus.PARTIALLY_FILLED:
            return self.state_machine.transition(
                ExecutionState.PARTIALLY_FILLED,
                reason="entry partial fill reconciled; new entries blocked",
                updates=updates,
            )
        if order.status is OrderStatus.NEW:
            return snapshot
        raise ReconciliationFailed(f"entry order status {order.status.value} is unsafe")

    def _apply_close_order(self, order: Order) -> ExecutionSnapshot:
        snapshot = self._snapshot()
        intent = snapshot.intent
        expected_side = self._opposite_side(intent.side)
        if (
            snapshot.state is not ExecutionState.CLOSING
            or not snapshot.close_client_order_id
            or order.symbol != intent.symbol
            or order.client_order_id != snapshot.close_client_order_id
            or order.side is not expected_side
            or order.position_side is not intent.position_side
            or order.requested_quantity != snapshot.filled_quantity
        ):
            raise ReconciliationFailed(
                "exchange close order ownership or intent does not match"
            )
        updates = {
            "close_exchange_order_id": order.order_id,
            "close_filled_quantity": order.executed_quantity,
            "close_average_price": order.average_price,
        }
        if order.status is OrderStatus.FILLED:
            return self.state_machine.transition(
                ExecutionState.CLOSED,
                reason="close fully filled",
                updates=updates,
            )
        if order.status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}:
            return self.state_machine.transition(
                ExecutionState.CLOSING,
                reason="close remains open or partially filled",
                updates=updates,
            )
        raise ReconciliationFailed(f"close order status {order.status.value} is unsafe")

    @staticmethod
    def _require_open_authorization(
        intent: TradeIntent,
        authorization: RiskAuthorization,
    ) -> None:
        ExecutionCoordinator._require_authorization(
            intent,
            authorization,
            expected_action="OPEN",
        )

    @staticmethod
    def _require_authorization(
        intent: TradeIntent,
        authorization: RiskAuthorization,
        *,
        expected_action: str,
    ) -> None:
        action = getattr(authorization.action, "value", authorization.action)
        if not authorization.allowed:
            raise RiskRejected("risk authorization denied execution")
        if action != expected_action:
            raise RiskRejected(
                f"risk authorization is not for {expected_action.lower()}"
            )
        if (
            authorization.strategy_id,
            authorization.run_id,
            authorization.intent_id,
        ) != (intent.strategy_id, intent.run_id, intent.intent_id):
            raise RiskRejected("risk authorization ownership does not match intent")
        if not authorization.policy_fingerprint:
            raise RiskRejected("risk authorization is missing policy fingerprint")

    @staticmethod
    def _opposite_side(side: OrderSide) -> OrderSide:
        return OrderSide.SELL if side is OrderSide.BUY else OrderSide.BUY

    def _snapshot(self) -> ExecutionSnapshot:
        snapshot = self.state_machine.snapshot
        if snapshot is None:
            raise ReconciliationFailed("execution state is missing")
        return snapshot

    def _confirm_owned_order(
        self,
        client_order_id: str,
        exchange_order_id: str,
    ) -> None:
        if self.ownership_sink is not None:
            self.ownership_sink.confirm_order(
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
            )

    def _prepare_protection_ownership(
        self,
        snapshot: ExecutionSnapshot,
        stop_id: str,
        take_id: str,
    ) -> None:
        if self.ownership_sink is None:
            return
        created_at = self._last_event_at()
        self.ownership_sink.prepare_order(
            intent_id=snapshot.intent.intent_id,
            role="STOP",
            client_order_id=stop_id,
            is_algo=True,
            created_at=created_at,
        )
        self.ownership_sink.prepare_order(
            intent_id=snapshot.intent.intent_id,
            role="TAKE_PROFIT",
            client_order_id=take_id,
            is_algo=True,
            created_at=created_at,
        )

    def _last_event_at(self) -> datetime:
        events = self.state_machine.ledger.load()
        if not events:
            raise ReconciliationFailed("execution event timestamp is missing")
        occurred_at = events[-1].occurred_at
        return occurred_at.astimezone(timezone.utc)
