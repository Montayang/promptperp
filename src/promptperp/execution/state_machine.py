from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from promptperp.domain import OrderSide, PositionSide
from promptperp.execution.ids import client_order_id
from promptperp.execution.lease import LeaseUnavailable, RunLease
from promptperp.execution.ledger import EventLedger, LedgerCorrupted
from promptperp.execution.models import ExecutionSnapshot, ExecutionState, TradeIntent

_ALLOWED_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.INTENT: frozenset(
        {ExecutionState.SUBMITTED, ExecutionState.BLOCKED}
    ),
    ExecutionState.SUBMITTED: frozenset(
        {
            ExecutionState.ACKNOWLEDGED,
            ExecutionState.PARTIALLY_FILLED,
            ExecutionState.FILLED,
            ExecutionState.BLOCKED,
        }
    ),
    ExecutionState.ACKNOWLEDGED: frozenset(
        {
            ExecutionState.PARTIALLY_FILLED,
            ExecutionState.FILLED,
            ExecutionState.BLOCKED,
        }
    ),
    ExecutionState.PARTIALLY_FILLED: frozenset(
        {
            ExecutionState.PARTIALLY_FILLED,
            ExecutionState.FILLED,
            ExecutionState.BLOCKED,
        }
    ),
    ExecutionState.FILLED: frozenset(
        {ExecutionState.PROTECTING, ExecutionState.CLOSING, ExecutionState.BLOCKED}
    ),
    ExecutionState.PROTECTING: frozenset(
        {ExecutionState.PROTECTED, ExecutionState.CLOSING, ExecutionState.BLOCKED}
    ),
    ExecutionState.PROTECTED: frozenset(
        {ExecutionState.CLOSING, ExecutionState.BLOCKED}
    ),
    ExecutionState.CLOSING: frozenset(
        {ExecutionState.CLOSING, ExecutionState.CLOSED, ExecutionState.BLOCKED}
    ),
    ExecutionState.CLOSED: frozenset(
        {ExecutionState.CLOSED, ExecutionState.SETTLED, ExecutionState.BLOCKED}
    ),
    ExecutionState.SETTLED: frozenset(),
    ExecutionState.BLOCKED: frozenset(),
}


class InvalidTransition(RuntimeError):
    pass


class ExecutionStateMachine:
    def __init__(self, ledger: EventLedger, *, lease: RunLease | None = None):
        self.ledger = ledger
        self.lease = lease
        self._snapshot = self._replay()

    @property
    def snapshot(self) -> ExecutionSnapshot | None:
        return self._snapshot

    def create(self, intent: TradeIntent) -> ExecutionSnapshot:
        self._require_writer(intent.strategy_id, intent.run_id)
        if self._snapshot is not None:
            raise InvalidTransition("execution intent already exists")
        entry_id = client_order_id(
            strategy_id=intent.strategy_id,
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            role="entry",
        )
        self.ledger.append(
            strategy_id=intent.strategy_id,
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            event_type="INTENT_CREATED",
            payload={
                "symbol": intent.symbol,
                "side": intent.side.value,
                "position_side": intent.position_side.value,
                "quantity": intent.quantity,
                "entry_client_order_id": entry_id,
            },
        )
        self._snapshot = self._replay()
        return self._require_snapshot()

    def transition(
        self,
        target: ExecutionState,
        *,
        reason: str,
        updates: Mapping[str, Any] | None = None,
    ) -> ExecutionSnapshot:
        snapshot = self._require_snapshot()
        self._require_writer(snapshot.intent.strategy_id, snapshot.intent.run_id)
        if target not in _ALLOWED_TRANSITIONS[snapshot.state]:
            raise InvalidTransition(
                f"{snapshot.state.value} cannot transition to {target.value}"
            )
        if not reason:
            raise ValueError("transition reason is required")
        payload = {
            "from": snapshot.state.value,
            "to": target.value,
            "reason": reason,
            "updates": dict(updates or {}),
        }
        self._apply_transition(snapshot, payload)
        self.ledger.append(
            strategy_id=snapshot.intent.strategy_id,
            run_id=snapshot.intent.run_id,
            intent_id=snapshot.intent.intent_id,
            event_type="STATE_TRANSITION",
            payload=payload,
        )
        self._snapshot = self._replay()
        return self._require_snapshot()

    def _replay(self) -> ExecutionSnapshot | None:
        snapshot: ExecutionSnapshot | None = None
        for event in self.ledger.load():
            if event.event_type == "INTENT_CREATED":
                if snapshot is not None or event.sequence != 1:
                    raise LedgerCorrupted("ledger contains duplicate intent creation")
                payload = event.payload
                try:
                    intent = TradeIntent(
                        strategy_id=event.strategy_id,
                        run_id=event.run_id,
                        intent_id=event.intent_id,
                        symbol=str(payload["symbol"]),
                        side=OrderSide(str(payload["side"])),
                        position_side=PositionSide(str(payload["position_side"])),
                        quantity=Decimal(str(payload["quantity"])),
                    )
                    snapshot = ExecutionSnapshot(
                        intent=intent,
                        state=ExecutionState.INTENT,
                        entry_client_order_id=str(payload["entry_client_order_id"]),
                        event_count=1,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise LedgerCorrupted("intent event is invalid") from exc
                continue
            if snapshot is None or event.event_type != "STATE_TRANSITION":
                raise LedgerCorrupted("ledger event order is invalid")
            if (
                event.strategy_id,
                event.run_id,
                event.intent_id,
            ) != (
                snapshot.intent.strategy_id,
                snapshot.intent.run_id,
                snapshot.intent.intent_id,
            ):
                raise LedgerCorrupted("ledger event ownership changed")
            snapshot = self._apply_transition(snapshot, event.payload)
        return snapshot

    @staticmethod
    def _apply_transition(
        snapshot: ExecutionSnapshot, payload: Mapping[str, Any]
    ) -> ExecutionSnapshot:
        try:
            source = ExecutionState(str(payload["from"]))
            target = ExecutionState(str(payload["to"]))
            reason = str(payload["reason"])
            if not reason:
                raise ValueError
            updates = payload.get("updates", {})
            if not isinstance(updates, dict):
                raise TypeError
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerCorrupted("transition event is invalid") from exc
        if source is not snapshot.state or target not in _ALLOWED_TRANSITIONS[source]:
            raise LedgerCorrupted("transition history is illegal")
        allowed_updates = {
            "exchange_order_id",
            "filled_quantity",
            "average_price",
            "stop_client_order_id",
            "take_profit_client_order_id",
            "stop_exchange_order_id",
            "take_profit_exchange_order_id",
            "close_client_order_id",
            "close_exchange_order_id",
            "close_filled_quantity",
            "close_average_price",
            "realized_pnl",
            "commission",
            "funding",
            "metadata",
        }
        if set(updates) - allowed_updates:
            raise LedgerCorrupted("transition contains unsupported fields")
        values = {
            "intent": snapshot.intent,
            "state": target,
            "entry_client_order_id": snapshot.entry_client_order_id,
            "exchange_order_id": updates.get(
                "exchange_order_id", snapshot.exchange_order_id
            ),
            "filled_quantity": Decimal(
                str(updates.get("filled_quantity", snapshot.filled_quantity))
            ),
            "average_price": Decimal(
                str(updates.get("average_price", snapshot.average_price))
            ),
            "stop_client_order_id": updates.get(
                "stop_client_order_id", snapshot.stop_client_order_id
            ),
            "take_profit_client_order_id": updates.get(
                "take_profit_client_order_id", snapshot.take_profit_client_order_id
            ),
            "stop_exchange_order_id": updates.get(
                "stop_exchange_order_id", snapshot.stop_exchange_order_id
            ),
            "take_profit_exchange_order_id": updates.get(
                "take_profit_exchange_order_id",
                snapshot.take_profit_exchange_order_id,
            ),
            "close_client_order_id": updates.get(
                "close_client_order_id", snapshot.close_client_order_id
            ),
            "close_exchange_order_id": updates.get(
                "close_exchange_order_id", snapshot.close_exchange_order_id
            ),
            "close_filled_quantity": Decimal(
                str(
                    updates.get("close_filled_quantity", snapshot.close_filled_quantity)
                )
            ),
            "close_average_price": Decimal(
                str(updates.get("close_average_price", snapshot.close_average_price))
            ),
            "realized_pnl": Decimal(
                str(updates.get("realized_pnl", snapshot.realized_pnl))
            ),
            "commission": Decimal(str(updates.get("commission", snapshot.commission))),
            "funding": Decimal(str(updates.get("funding", snapshot.funding))),
            "last_reason": reason,
            "event_count": snapshot.event_count + 1,
            "metadata": updates.get("metadata", snapshot.metadata),
        }
        try:
            return ExecutionSnapshot(**values)
        except (TypeError, ValueError) as exc:
            raise LedgerCorrupted("transition update is invalid") from exc

    def _require_snapshot(self) -> ExecutionSnapshot:
        if self._snapshot is None:
            raise InvalidTransition("execution intent has not been created")
        return self._snapshot

    def _require_writer(self, strategy_id: str, run_id: str) -> None:
        if self.lease is None or not self.lease.acquired:
            raise LeaseUnavailable("execution state writes require an active run lease")
        if (self.lease.strategy_id, self.lease.run_id) != (strategy_id, run_id):
            raise LeaseUnavailable("run lease ownership does not match execution state")
