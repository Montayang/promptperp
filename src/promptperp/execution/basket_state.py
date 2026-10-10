from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping

from promptperp.domain import PositionSide
from promptperp.execution.basket import (
    BasketAdjustment,
    BasketAdjustmentRole,
    BasketRebalancePlan,
)
from promptperp.execution.ids import client_order_id
from promptperp.execution.lease import LeaseUnavailable, RunLease
from promptperp.execution.ledger import EventLedger, LedgerCorrupted


class BasketExecutionError(RuntimeError):
    pass


class BasketPhase(str, Enum):
    PREPARED = "PREPARED"
    REDUCING = "REDUCING"
    INCREASING = "INCREASING"
    ROLLING_BACK = "ROLLING_BACK"
    SAFE_FLAT = "SAFE_FLAT"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"


class BasketLegState(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    FAILED = "FAILED"


_PHASE_TRANSITIONS: dict[BasketPhase, frozenset[BasketPhase]] = {
    BasketPhase.PREPARED: frozenset(
        {
            BasketPhase.REDUCING,
            BasketPhase.INCREASING,
            BasketPhase.COMPLETE,
            BasketPhase.ROLLING_BACK,
        }
    ),
    BasketPhase.REDUCING: frozenset(
        {
            BasketPhase.INCREASING,
            BasketPhase.COMPLETE,
            BasketPhase.BLOCKED,
            BasketPhase.ROLLING_BACK,
        }
    ),
    BasketPhase.INCREASING: frozenset({BasketPhase.COMPLETE, BasketPhase.ROLLING_BACK}),
    BasketPhase.ROLLING_BACK: frozenset({BasketPhase.SAFE_FLAT, BasketPhase.BLOCKED}),
    BasketPhase.SAFE_FLAT: frozenset(),
    BasketPhase.COMPLETE: frozenset({BasketPhase.ROLLING_BACK}),
    BasketPhase.BLOCKED: frozenset(),
}

_LEG_TRANSITIONS: dict[BasketLegState, frozenset[BasketLegState]] = {
    BasketLegState.PENDING: frozenset({BasketLegState.SUBMITTED}),
    BasketLegState.SUBMITTED: frozenset(
        {
            BasketLegState.SUBMITTED,
            BasketLegState.PARTIALLY_FILLED,
            BasketLegState.FILLED,
            BasketLegState.FAILED,
        }
    ),
    BasketLegState.PARTIALLY_FILLED: frozenset(
        {
            BasketLegState.PARTIALLY_FILLED,
            BasketLegState.FILLED,
            BasketLegState.FAILED,
        }
    ),
    BasketLegState.FILLED: frozenset(),
    BasketLegState.FAILED: frozenset(),
}


@dataclass(frozen=True)
class BasketExecutionLeg:
    index: int
    adjustment: BasketAdjustment
    client_order_id: str
    state: BasketLegState = BasketLegState.PENDING
    exchange_order_id: str | None = None
    filled_quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.index < 0 or not self.client_order_id:
            raise ValueError("basket leg identity is invalid")
        if not Decimal("0") <= self.filled_quantity <= self.adjustment.quantity:
            raise ValueError("basket leg fill is outside requested quantity")
        if not self.average_price.is_finite() or self.average_price < 0:
            raise ValueError("basket leg average price is invalid")
        if self.filled_quantity > 0 and self.average_price <= 0:
            raise ValueError("basket leg fill requires an average price")
        if self.state is BasketLegState.FILLED and (
            self.filled_quantity != self.adjustment.quantity
        ):
            raise ValueError("filled basket leg must reach requested quantity")


@dataclass(frozen=True)
class BasketExecutionSnapshot:
    strategy_id: str
    run_id: str
    basket_id: str
    target_fingerprint: str
    phase: BasketPhase
    legs: tuple[BasketExecutionLeg, ...]
    event_count: int
    failure_reason: str = ""


class BasketExecutionStateMachine:
    """Hash-chained, single-writer state for one close-first basket rebalance."""

    def __init__(self, ledger: EventLedger, *, lease: RunLease | None = None):
        self.ledger = ledger
        self.lease = lease
        self._snapshot = self._replay()

    @property
    def snapshot(self) -> BasketExecutionSnapshot | None:
        return self._snapshot

    def create(
        self,
        *,
        strategy_id: str,
        run_id: str,
        basket_id: str,
        target_fingerprint: str,
        plan: BasketRebalancePlan,
    ) -> BasketExecutionSnapshot:
        self._require_writer(strategy_id, run_id)
        if self._snapshot is not None:
            raise BasketExecutionError("basket execution already exists")
        if (
            not basket_id
            or len(target_fingerprint) != 64
            or any(
                character not in "0123456789abcdef" for character in target_fingerprint
            )
        ):
            raise ValueError("basket identity or target fingerprint is invalid")
        adjustments = plan.ordered
        legs = [
            {
                "index": index,
                "role": adjustment.role.value,
                "symbol": adjustment.symbol,
                "position_side": adjustment.position_side.value,
                "quantity": adjustment.quantity,
                "target_quantity": adjustment.target_quantity,
                "client_order_id": client_order_id(
                    strategy_id=strategy_id,
                    run_id=run_id,
                    intent_id=f"{basket_id}:{index}:{adjustment.symbol}",
                    role=adjustment.role.value.lower(),
                ),
            }
            for index, adjustment in enumerate(adjustments)
        ]
        self.ledger.append(
            strategy_id=strategy_id,
            run_id=run_id,
            intent_id=basket_id,
            event_type="BASKET_CREATED",
            payload={"target_fingerprint": target_fingerprint, "legs": legs},
        )
        self._snapshot = self._replay()
        return self._required()

    def start(self) -> BasketExecutionSnapshot:
        snapshot = self._required()
        if snapshot.phase is not BasketPhase.PREPARED:
            raise BasketExecutionError("basket is not prepared")
        if any(
            leg.adjustment.role is BasketAdjustmentRole.REDUCE for leg in snapshot.legs
        ):
            target = BasketPhase.REDUCING
        elif snapshot.legs:
            target = BasketPhase.INCREASING
        else:
            target = BasketPhase.COMPLETE
        return self._phase(target, "basket execution started")

    def next_leg(self) -> BasketExecutionLeg | None:
        snapshot = self._required()
        role = {
            BasketPhase.REDUCING: BasketAdjustmentRole.REDUCE,
            BasketPhase.INCREASING: BasketAdjustmentRole.INCREASE,
        }.get(snapshot.phase)
        if role is None:
            return None
        candidates = [
            leg
            for leg in snapshot.legs
            if leg.adjustment.role is role
            and leg.state
            in {
                BasketLegState.PENDING,
                BasketLegState.SUBMITTED,
                BasketLegState.PARTIALLY_FILLED,
            }
        ]
        return min(candidates, key=lambda leg: leg.index) if candidates else None

    def resume(self) -> BasketExecutionSnapshot:
        """Finish a durable phase advance interrupted after its final fill event."""

        snapshot = self._required()
        if snapshot.phase not in {BasketPhase.REDUCING, BasketPhase.INCREASING}:
            return snapshot
        self._advance_if_complete(snapshot)
        return self._required()

    def mark_submitted(self, index: int) -> BasketExecutionSnapshot:
        snapshot = self._required()
        leg = self._leg(snapshot, index)
        if self.next_leg() != leg or leg.state is not BasketLegState.PENDING:
            raise BasketExecutionError("basket leg is not next for submission")
        self._append_leg(
            snapshot,
            leg,
            BasketLegState.SUBMITTED,
            reason="order submission started",
        )
        return self._refresh()

    def confirm(
        self,
        index: int,
        *,
        exchange_order_id: str,
        cumulative_filled: Decimal,
        average_price: Decimal,
    ) -> BasketExecutionSnapshot:
        snapshot = self._required()
        leg = self._leg(snapshot, index)
        if leg.state not in {
            BasketLegState.SUBMITTED,
            BasketLegState.PARTIALLY_FILLED,
        }:
            raise BasketExecutionError("basket leg is not awaiting confirmation")
        if not exchange_order_id:
            raise ValueError("exchange order identity is required")
        if not cumulative_filled.is_finite() or not average_price.is_finite():
            raise ValueError("basket leg confirmation values must be finite")
        if cumulative_filled < 0 or average_price < 0:
            raise ValueError("basket leg confirmation values cannot be negative")
        if (cumulative_filled == 0) != (average_price == 0):
            raise ValueError(
                "basket leg fill and average price must become positive together"
            )
        if leg.exchange_order_id not in (None, exchange_order_id):
            raise BasketExecutionError("basket leg exchange identity changed")
        if cumulative_filled < leg.filled_quantity:
            raise BasketExecutionError("basket leg cumulative fill moved backwards")
        if cumulative_filled > leg.adjustment.quantity:
            raise BasketExecutionError("basket leg overfilled")
        if cumulative_filled == leg.filled_quantity and (
            leg.exchange_order_id == exchange_order_id
            and leg.average_price == average_price
        ):
            return snapshot
        target = (
            BasketLegState.FILLED
            if cumulative_filled == leg.adjustment.quantity
            else (
                BasketLegState.PARTIALLY_FILLED
                if cumulative_filled > 0
                else BasketLegState.SUBMITTED
            )
        )
        self._append_leg(
            snapshot,
            leg,
            target,
            reason="exchange order reconciled",
            exchange_order_id=exchange_order_id,
            filled_quantity=cumulative_filled,
            average_price=average_price,
        )
        snapshot = self._refresh()
        if target is BasketLegState.FILLED:
            self._advance_if_complete(snapshot)
        return self._required()

    def fail_leg(self, index: int, *, reason: str) -> BasketExecutionSnapshot:
        snapshot = self._required()
        leg = self._leg(snapshot, index)
        if not reason or leg.state not in {
            BasketLegState.SUBMITTED,
            BasketLegState.PARTIALLY_FILLED,
        }:
            raise BasketExecutionError("basket leg cannot be failed")
        self._append_leg(snapshot, leg, BasketLegState.FAILED, reason=reason)
        snapshot = self._refresh()
        phase = (
            BasketPhase.ROLLING_BACK
            if leg.adjustment.role is BasketAdjustmentRole.INCREASE
            else BasketPhase.BLOCKED
        )
        return self._phase(phase, reason)

    def confirm_safe_flat(
        self,
        remaining_quantities: Mapping[tuple[str, PositionSide], Decimal],
    ) -> BasketExecutionSnapshot:
        snapshot = self._required()
        if snapshot.phase is not BasketPhase.ROLLING_BACK:
            raise BasketExecutionError("basket is not rolling back")
        if any(quantity != 0 for quantity in remaining_quantities.values()):
            raise BasketExecutionError("strategy is not flat after rollback")
        return self._phase(
            BasketPhase.SAFE_FLAT,
            "strategy rollback confirmed flat",
            evidence={"remaining_quantities": []},
        )

    def begin_safe_flatten(self, *, reason: str) -> BasketExecutionSnapshot:
        snapshot = self._required()
        if not reason:
            raise BasketExecutionError("safe flatten requires an audited reason")
        if snapshot.phase in {BasketPhase.ROLLING_BACK, BasketPhase.SAFE_FLAT}:
            return snapshot
        if any(
            leg.state in {BasketLegState.SUBMITTED, BasketLegState.PARTIALLY_FILLED}
            for leg in snapshot.legs
        ):
            raise BasketExecutionError(
                "pending orders must be reconciled before safe flatten"
            )
        return self._phase(
            BasketPhase.ROLLING_BACK, reason, evidence={"safe_flatten_requested": True}
        )

    def _advance_if_complete(self, snapshot: BasketExecutionSnapshot) -> None:
        if snapshot.phase is BasketPhase.REDUCING:
            reductions = [
                leg
                for leg in snapshot.legs
                if leg.adjustment.role is BasketAdjustmentRole.REDUCE
            ]
            if all(leg.state is BasketLegState.FILLED for leg in reductions):
                target = (
                    BasketPhase.INCREASING
                    if any(
                        leg.adjustment.role is BasketAdjustmentRole.INCREASE
                        for leg in snapshot.legs
                    )
                    else BasketPhase.COMPLETE
                )
                self._phase(target, "all reductions confirmed")
        elif snapshot.phase is BasketPhase.INCREASING:
            increases = [
                leg
                for leg in snapshot.legs
                if leg.adjustment.role is BasketAdjustmentRole.INCREASE
            ]
            if all(leg.state is BasketLegState.FILLED for leg in increases):
                self._phase(BasketPhase.COMPLETE, "all increases confirmed")

    def _phase(
        self,
        target: BasketPhase,
        reason: str,
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> BasketExecutionSnapshot:
        snapshot = self._required()
        if target not in _PHASE_TRANSITIONS[snapshot.phase]:
            raise BasketExecutionError(
                f"{snapshot.phase.value} cannot transition to {target.value}"
            )
        self._require_writer(snapshot.strategy_id, snapshot.run_id)
        self.ledger.append(
            strategy_id=snapshot.strategy_id,
            run_id=snapshot.run_id,
            intent_id=snapshot.basket_id,
            event_type="BASKET_PHASE",
            payload={
                "from": snapshot.phase.value,
                "to": target.value,
                "reason": reason,
                "evidence": dict(evidence or {}),
            },
        )
        return self._refresh()

    def _append_leg(
        self,
        snapshot: BasketExecutionSnapshot,
        leg: BasketExecutionLeg,
        target: BasketLegState,
        *,
        reason: str,
        exchange_order_id: str | None = None,
        filled_quantity: Decimal | None = None,
        average_price: Decimal | None = None,
    ) -> None:
        self._require_writer(snapshot.strategy_id, snapshot.run_id)
        self.ledger.append(
            strategy_id=snapshot.strategy_id,
            run_id=snapshot.run_id,
            intent_id=snapshot.basket_id,
            event_type="BASKET_LEG",
            payload={
                "index": leg.index,
                "from": leg.state.value,
                "to": target.value,
                "reason": reason,
                "exchange_order_id": exchange_order_id or leg.exchange_order_id,
                "filled_quantity": filled_quantity
                if filled_quantity is not None
                else leg.filled_quantity,
                "average_price": average_price
                if average_price is not None
                else leg.average_price,
            },
        )

    def _replay(self) -> BasketExecutionSnapshot | None:
        snapshot: BasketExecutionSnapshot | None = None
        for event in self.ledger.load():
            if event.event_type == "BASKET_CREATED":
                if snapshot is not None or event.sequence != 1:
                    raise LedgerCorrupted("basket creation is duplicated")
                snapshot = self._created_snapshot(
                    event.strategy_id, event.run_id, event.intent_id, event.payload
                )
                continue
            if snapshot is None or (
                event.strategy_id,
                event.run_id,
                event.intent_id,
            ) != (
                snapshot.strategy_id,
                snapshot.run_id,
                snapshot.basket_id,
            ):
                raise LedgerCorrupted("basket event ownership changed")
            if event.event_type == "BASKET_PHASE":
                snapshot = self._apply_phase(snapshot, event.payload)
            elif event.event_type == "BASKET_LEG":
                snapshot = self._apply_leg(snapshot, event.payload)
            else:
                raise LedgerCorrupted("basket event type is invalid")
            snapshot = BasketExecutionSnapshot(
                **{**snapshot.__dict__, "event_count": event.sequence}
            )
        return snapshot

    @staticmethod
    def _created_snapshot(
        strategy_id: str, run_id: str, basket_id: str, payload: Mapping[str, Any]
    ) -> BasketExecutionSnapshot:
        try:
            legs = tuple(
                BasketExecutionLeg(
                    index=int(raw["index"]),
                    adjustment=BasketAdjustment(
                        role=BasketAdjustmentRole(str(raw["role"])),
                        symbol=str(raw["symbol"]),
                        position_side=PositionSide(str(raw["position_side"])),
                        quantity=Decimal(str(raw["quantity"])),
                        target_quantity=Decimal(str(raw["target_quantity"])),
                    ),
                    client_order_id=str(raw["client_order_id"]),
                )
                for raw in payload["legs"]
            )
            fingerprint = str(payload["target_fingerprint"])
            if (
                len(fingerprint) != 64
                or any(character not in "0123456789abcdef" for character in fingerprint)
                or tuple(leg.index for leg in legs) != tuple(range(len(legs)))
            ):
                raise ValueError
            return BasketExecutionSnapshot(
                strategy_id=strategy_id,
                run_id=run_id,
                basket_id=basket_id,
                target_fingerprint=fingerprint,
                phase=BasketPhase.PREPARED,
                legs=legs,
                event_count=1,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerCorrupted("basket creation event is invalid") from exc

    @staticmethod
    def _apply_phase(
        snapshot: BasketExecutionSnapshot, payload: Mapping[str, Any]
    ) -> BasketExecutionSnapshot:
        try:
            source = BasketPhase(str(payload["from"]))
            target = BasketPhase(str(payload["to"]))
            reason = str(payload["reason"])
            evidence = payload.get("evidence", {})
            if not isinstance(evidence, dict):
                raise TypeError
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerCorrupted("basket phase event is invalid") from exc
        if (
            source is not snapshot.phase
            or target not in _PHASE_TRANSITIONS[source]
            or not reason
        ):
            raise LedgerCorrupted("basket phase history is invalid")
        BasketExecutionStateMachine._validate_phase_semantics(
            snapshot, target, evidence
        )
        return BasketExecutionSnapshot(
            **{
                **snapshot.__dict__,
                "phase": target,
                "failure_reason": reason
                if target in {BasketPhase.ROLLING_BACK, BasketPhase.BLOCKED}
                else snapshot.failure_reason,
            }
        )

    @staticmethod
    def _apply_leg(
        snapshot: BasketExecutionSnapshot, payload: Mapping[str, Any]
    ) -> BasketExecutionSnapshot:
        try:
            index = int(payload["index"])
            source = BasketLegState(str(payload["from"]))
            target = BasketLegState(str(payload["to"]))
            reason = str(payload["reason"])
            leg = snapshot.legs[index]
            expected_phase = (
                BasketPhase.REDUCING
                if leg.adjustment.role is BasketAdjustmentRole.REDUCE
                else BasketPhase.INCREASING
            )
            if (
                leg.index != index
                or leg.state is not source
                or target not in _LEG_TRANSITIONS[source]
                or snapshot.phase is not expected_phase
                or not reason
            ):
                raise ValueError
            replacement = BasketExecutionLeg(
                index=leg.index,
                adjustment=leg.adjustment,
                client_order_id=leg.client_order_id,
                state=target,
                exchange_order_id=(
                    None
                    if payload.get("exchange_order_id") is None
                    else str(payload["exchange_order_id"])
                ),
                filled_quantity=Decimal(str(payload["filled_quantity"])),
                average_price=Decimal(str(payload["average_price"])),
            )
        except (IndexError, InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise LedgerCorrupted("basket leg event is invalid") from exc
        legs = list(snapshot.legs)
        legs[index] = replacement
        return BasketExecutionSnapshot(**{**snapshot.__dict__, "legs": tuple(legs)})

    @staticmethod
    def _validate_phase_semantics(
        snapshot: BasketExecutionSnapshot,
        target: BasketPhase,
        evidence: Mapping[str, Any],
    ) -> None:
        reductions = tuple(
            leg
            for leg in snapshot.legs
            if leg.adjustment.role is BasketAdjustmentRole.REDUCE
        )
        increases = tuple(
            leg
            for leg in snapshot.legs
            if leg.adjustment.role is BasketAdjustmentRole.INCREASE
        )
        valid = True
        if (
            target is BasketPhase.ROLLING_BACK
            and evidence.get("safe_flatten_requested") is True
        ):
            if any(
                leg.state in {BasketLegState.SUBMITTED, BasketLegState.PARTIALLY_FILLED}
                for leg in snapshot.legs
            ):
                raise LedgerCorrupted("safe flatten history retains unresolved orders")
            return
        if snapshot.phase is BasketPhase.PREPARED:
            expected = (
                BasketPhase.REDUCING
                if reductions
                else BasketPhase.INCREASING
                if increases
                else BasketPhase.COMPLETE
            )
            valid = target is expected
        elif snapshot.phase is BasketPhase.REDUCING:
            if target is BasketPhase.BLOCKED:
                valid = any(leg.state is BasketLegState.FAILED for leg in reductions)
            else:
                expected = BasketPhase.INCREASING if increases else BasketPhase.COMPLETE
                valid = target is expected and all(
                    leg.state is BasketLegState.FILLED for leg in reductions
                )
        elif snapshot.phase is BasketPhase.INCREASING:
            if target is BasketPhase.ROLLING_BACK:
                valid = any(leg.state is BasketLegState.FAILED for leg in increases)
            elif target is BasketPhase.COMPLETE:
                valid = all(leg.state is BasketLegState.FILLED for leg in increases)
            else:
                valid = False
        elif snapshot.phase is BasketPhase.ROLLING_BACK:
            if target is BasketPhase.SAFE_FLAT:
                valid = evidence.get("remaining_quantities") == []
            elif target is BasketPhase.BLOCKED:
                valid = True
            else:
                valid = False
        if not valid:
            raise LedgerCorrupted("basket phase transition lacks required evidence")

    @staticmethod
    def _leg(snapshot: BasketExecutionSnapshot, index: int) -> BasketExecutionLeg:
        try:
            leg = snapshot.legs[index]
        except IndexError as exc:
            raise BasketExecutionError("basket leg index is invalid") from exc
        if leg.index != index:
            raise BasketExecutionError("basket leg index is invalid")
        return leg

    def _refresh(self) -> BasketExecutionSnapshot:
        self._snapshot = self._replay()
        return self._required()

    def _required(self) -> BasketExecutionSnapshot:
        if self._snapshot is None:
            raise BasketExecutionError("basket execution does not exist")
        return self._snapshot

    def _require_writer(self, strategy_id: str, run_id: str) -> None:
        if self.lease is None or not self.lease.acquired:
            raise LeaseUnavailable("basket state writes require an active run lease")
        if (self.lease.strategy_id, self.lease.run_id) != (strategy_id, run_id):
            raise LeaseUnavailable("run lease ownership does not match basket state")
