from __future__ import annotations

from decimal import Decimal

import pytest

from promptperp.domain import OrderSide, PositionSide, ReconciliationFailed
from promptperp.execution import (
    ExecutionRegistry,
    ExecutionState,
    RunLease,
    TradeIntent,
)


class NoWriteGateway:
    def place_market_order(self, **_kwargs):
        raise AssertionError("registry tests must not place orders")

    def get_order(self, **_kwargs):
        raise AssertionError("registry tests must not query orders")

    def cancel_order(self, **_kwargs):
        raise AssertionError("registry tests must not cancel orders")


def trade_intent(intent_id):
    return TradeIntent(
        strategy_id="sample",
        run_id="run-1",
        intent_id=intent_id,
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        quantity=Decimal("1"),
    )


def registry(tmp_path):
    lease = RunLease(
        tmp_path / "run.lock",
        strategy_id="sample",
        run_id="run-1",
        owner_id="test",
    )
    lease.acquire()
    return ExecutionRegistry(
        tmp_path / "executions",
        lease=lease,
        execution_gateway=NoWriteGateway(),
    )


def test_registry_uses_path_safe_content_addressed_intent_ledgers(tmp_path):
    executions = registry(tmp_path)
    hostile_id = "../../outside/intent"
    coordinator = executions.create(trade_intent(hostile_id))
    coordinator.state_machine.create(trade_intent(hostile_id))

    files = tuple((tmp_path / "executions").glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].parent == tmp_path / "executions"
    assert hostile_id not in files[0].name
    assert (
        executions.load(hostile_id).state_machine.snapshot.intent.intent_id
        == hostile_id
    )


def test_registry_blocks_second_intent_until_first_is_settled(tmp_path):
    executions = registry(tmp_path)
    first = trade_intent("first")
    coordinator = executions.create(first)
    coordinator.state_machine.create(first)

    with pytest.raises(ReconciliationFailed, match="unresolved"):
        executions.create(trade_intent("second"))

    settle_machine = coordinator.state_machine
    settle_machine.transition(ExecutionState.SUBMITTED, reason="test")
    settle_machine.transition(
        ExecutionState.FILLED,
        reason="test",
        updates={"filled_quantity": Decimal("1"), "average_price": Decimal("100")},
    )
    settle_machine.transition(
        ExecutionState.CLOSING,
        reason="test",
        updates={"close_client_order_id": "close-1"},
    )
    settle_machine.transition(
        ExecutionState.CLOSED,
        reason="test",
        updates={
            "close_exchange_order_id": "exchange-close-1",
            "close_filled_quantity": Decimal("1"),
            "close_average_price": Decimal("101"),
        },
    )
    settle_machine.transition(ExecutionState.SETTLED, reason="test")

    second = executions.create(trade_intent("second"))
    assert second.state_machine.snapshot is None


def test_registry_rejects_missing_wrong_run_and_released_lease(tmp_path):
    executions = registry(tmp_path)
    with pytest.raises(ReconciliationFailed, match="missing"):
        executions.load("unknown")

    with pytest.raises(ReconciliationFailed, match="ownership"):
        executions.create(
            TradeIntent(
                strategy_id="foreign",
                run_id="run-1",
                intent_id="foreign",
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                position_side=PositionSide.LONG,
                quantity=Decimal("1"),
            )
        )

    executions.lease.release()
    with pytest.raises(ReconciliationFailed, match="active run lease"):
        executions.snapshots()
