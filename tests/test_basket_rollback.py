from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from promptperp.domain import PositionSide
from promptperp.execution.basket import (
    BasketAdjustment,
    BasketAdjustmentRole,
    BasketRebalancePlan,
)
from promptperp.execution.basket_rollback import BasketRollbackManager
from promptperp.execution.basket_state import (
    BasketExecutionError,
    BasketExecutionStateMachine,
    BasketPhase,
)
from promptperp.execution.lease import RunLease
from promptperp.execution.ledger import EventLedger
from promptperp.execution.virtual_positions import VirtualPositionStore

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def _failed_machine(tmp_path, lease):
    machine = BasketExecutionStateMachine(
        EventLedger(tmp_path / "failed.jsonl"), lease=lease
    )
    machine.create(
        strategy_id="strategy_beta",
        run_id="run-1",
        basket_id="daily-1",
        target_fingerprint="a" * 64,
        plan=BasketRebalancePlan(
            reductions=(),
            increases=(
                BasketAdjustment(
                    role=BasketAdjustmentRole.INCREASE,
                    symbol="BTCUSDT",
                    position_side=PositionSide.LONG,
                    quantity=Decimal("1"),
                    target_quantity=Decimal("1"),
                ),
            ),
        ),
    )
    machine.start()
    machine.mark_submitted(0)
    machine.confirm(
        0,
        exchange_order_id="failed-order",
        cumulative_filled=Decimal("0.4"),
        average_price=Decimal("100"),
    )
    machine.fail_leg(0, reason="remainder canceled")
    return machine


def test_rollback_plan_contains_only_failed_strategy_owned_exposure(tmp_path) -> None:
    lease = RunLease(
        tmp_path / "run.lock",
        strategy_id="strategy_beta",
        run_id="run-1",
        owner_id="test-worker",
    )
    lease.acquire()
    failed = _failed_machine(tmp_path, lease)
    positions = VirtualPositionStore(tmp_path / "positions.sqlite3")
    positions.initialize()
    positions.open_fill(
        event_id="strategy_beta-fill",
        strategy_id="strategy_beta",
        run_id="run-1",
        symbol="BTCUSDT",
        position_side=PositionSide.LONG,
        quantity=Decimal("0.4"),
        price=Decimal("100"),
        commission=Decimal("0.01"),
        occurred_at=NOW,
    )
    positions.open_fill(
        event_id="other-fill",
        strategy_id="strategy_alpha",
        run_id="strategy_alpha-run",
        symbol="BTCUSDT",
        position_side=PositionSide.LONG,
        quantity=Decimal("2"),
        price=Decimal("100"),
        commission=Decimal("0.01"),
        occurred_at=NOW,
    )
    rollback = BasketExecutionStateMachine(
        EventLedger(tmp_path / "rollback.jsonl"), lease=lease
    )
    manager = BasketRollbackManager(
        failed=failed, rollback=rollback, positions=positions
    )

    prepared = manager.prepare()

    assert prepared.basket_id == "daily-1:rollback"
    assert len(prepared.legs) == 1
    assert prepared.legs[0].adjustment.role is BasketAdjustmentRole.REDUCE
    assert prepared.legs[0].adjustment.quantity == Decimal("0.4")
    assert positions.position(
        "strategy_alpha", "strategy_alpha-run", "BTCUSDT", PositionSide.LONG
    ).quantity == Decimal("2")
    lease.release()


def test_rollback_finalizes_only_after_child_complete_and_virtual_flat(
    tmp_path,
) -> None:
    lease = RunLease(
        tmp_path / "run.lock",
        strategy_id="strategy_beta",
        run_id="run-1",
        owner_id="test-worker",
    )
    lease.acquire()
    failed = _failed_machine(tmp_path, lease)
    positions = VirtualPositionStore(tmp_path / "positions.sqlite3")
    positions.initialize()
    positions.open_fill(
        event_id="strategy_beta-fill",
        strategy_id="strategy_beta",
        run_id="run-1",
        symbol="BTCUSDT",
        position_side=PositionSide.LONG,
        quantity=Decimal("0.4"),
        price=Decimal("100"),
        commission=Decimal("0.01"),
        occurred_at=NOW,
    )
    rollback = BasketExecutionStateMachine(
        EventLedger(tmp_path / "rollback.jsonl"), lease=lease
    )
    manager = BasketRollbackManager(
        failed=failed, rollback=rollback, positions=positions
    )
    manager.prepare()

    with pytest.raises(BasketExecutionError, match="not complete"):
        manager.finalize()
    rollback.start()
    rollback.mark_submitted(0)
    rollback.confirm(
        0,
        exchange_order_id="rollback-order",
        cumulative_filled=Decimal("0.4"),
        average_price=Decimal("99"),
    )
    with pytest.raises(BasketExecutionError, match="not flat"):
        manager.finalize()
    positions.close_fill(
        event_id="rollback-fill",
        strategy_id="strategy_beta",
        run_id="run-1",
        symbol="BTCUSDT",
        position_side=PositionSide.LONG,
        quantity=Decimal("0.4"),
        price=Decimal("99"),
        commission=Decimal("0.01"),
        occurred_at=NOW,
    )

    assert manager.prepare().phase is BasketPhase.COMPLETE
    assert manager.finalize().phase is BasketPhase.SAFE_FLAT
    lease.release()
