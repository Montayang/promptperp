from __future__ import annotations

import hashlib
import json

from promptperp.execution.basket import BasketRebalancePlan, plan_safe_flatten
from promptperp.execution.basket_state import (
    BasketExecutionError,
    BasketExecutionSnapshot,
    BasketExecutionStateMachine,
    BasketPhase,
)
from promptperp.execution.virtual_positions import VirtualPositionStore


class BasketRollbackManager:
    """Build and finalize a reduce-only child basket for one failed basket."""

    def __init__(
        self,
        *,
        failed: BasketExecutionStateMachine,
        rollback: BasketExecutionStateMachine,
        positions: VirtualPositionStore,
    ):
        self.failed = failed
        self.rollback = rollback
        self.positions = positions

    def prepare(self) -> BasketExecutionSnapshot:
        failed = self._failed_snapshot()
        rollback_id = f"{failed.basket_id}:rollback"
        current = self.rollback.snapshot
        if current is not None:
            if (current.strategy_id, current.run_id, current.basket_id) != (
                failed.strategy_id,
                failed.run_id,
                rollback_id,
            ):
                raise BasketExecutionError("rollback basket ownership changed")
            return current
        quantities = self.positions.quantities(
            strategy_id=failed.strategy_id,
            run_id=failed.run_id,
        )
        plan = plan_safe_flatten(current_quantities=quantities)
        fingerprint = self._fingerprint(plan)
        return self.rollback.create(
            strategy_id=failed.strategy_id,
            run_id=failed.run_id,
            basket_id=rollback_id,
            target_fingerprint=fingerprint,
            plan=plan,
        )

    def finalize(self) -> BasketExecutionSnapshot:
        failed = self._failed_snapshot()
        rollback = self.rollback.snapshot
        if rollback is None or rollback.phase is not BasketPhase.COMPLETE:
            raise BasketExecutionError("rollback basket is not complete")
        quantities = self.positions.quantities(
            strategy_id=failed.strategy_id,
            run_id=failed.run_id,
        )
        return self.failed.confirm_safe_flat(quantities)

    def _failed_snapshot(self) -> BasketExecutionSnapshot:
        snapshot = self.failed.snapshot
        if snapshot is None or snapshot.phase is not BasketPhase.ROLLING_BACK:
            raise BasketExecutionError("failed basket is not awaiting rollback")
        return snapshot

    @staticmethod
    def _fingerprint(plan: BasketRebalancePlan) -> str:
        payload = [
            {
                "role": leg.role.value,
                "symbol": leg.symbol,
                "position_side": leg.position_side.value,
                "quantity": str(leg.quantity),
                "target_quantity": str(leg.target_quantity),
            }
            for leg in plan.ordered
        ]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()
