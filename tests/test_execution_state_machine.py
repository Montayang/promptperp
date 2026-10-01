from __future__ import annotations

import json
from decimal import Decimal

import pytest

from promptperp.domain import OrderSide, PositionSide
from promptperp.execution import (
    EventLedger,
    ExecutionState,
    ExecutionStateMachine,
    LeaseUnavailable,
    LedgerCorrupted,
    RunLease,
    TradeIntent,
    client_order_id,
)
from promptperp.execution.state_machine import InvalidTransition


def intent() -> TradeIntent:
    return TradeIntent(
        strategy_id="sample",
        run_id="run-001",
        intent_id="intent-001",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        quantity=Decimal("0.125"),
    )


def writer_machine(path):
    lease = RunLease(
        path.with_suffix(".lock"),
        strategy_id="sample",
        run_id="run-001",
        owner_id="test-worker",
    )
    lease.acquire()
    return ExecutionStateMachine(EventLedger(path), lease=lease)


def test_client_order_id_is_deterministic_bounded_and_role_scoped():
    entry = client_order_id(
        strategy_id="sample",
        run_id="run-001",
        intent_id="intent-001",
        role="entry",
    )

    assert entry == client_order_id(
        strategy_id="sample",
        run_id="run-001",
        intent_id="intent-001",
        role="entry",
    )
    assert entry != client_order_id(
        strategy_id="sample",
        run_id="run-001",
        intent_id="intent-001",
        role="stop",
    )
    assert len(entry) <= 36
    assert entry.startswith("bb-entry-")


def test_every_state_transition_replays_after_restart(tmp_path):
    path = tmp_path / "execution.jsonl"
    machine = writer_machine(path)
    snapshot = machine.create(intent())
    assert snapshot.state is ExecutionState.INTENT

    transitions = [
        ExecutionState.SUBMITTED,
        ExecutionState.ACKNOWLEDGED,
        ExecutionState.PARTIALLY_FILLED,
        ExecutionState.FILLED,
        ExecutionState.PROTECTING,
        ExecutionState.PROTECTED,
        ExecutionState.CLOSING,
        ExecutionState.CLOSED,
        ExecutionState.SETTLED,
    ]
    for target in transitions:
        updates = {}
        if target is ExecutionState.ACKNOWLEDGED:
            updates["exchange_order_id"] = "exchange-1"
        if target is ExecutionState.PARTIALLY_FILLED:
            updates["filled_quantity"] = Decimal("0.05")
        if target is ExecutionState.FILLED:
            updates["filled_quantity"] = Decimal("0.125")
            updates["average_price"] = Decimal("100")
        if target is ExecutionState.PROTECTING:
            updates["stop_client_order_id"] = "stop-client-1"
            updates["take_profit_client_order_id"] = "take-client-1"
        if target is ExecutionState.PROTECTED:
            updates["stop_exchange_order_id"] = "stop-exchange-1"
            updates["take_profit_exchange_order_id"] = "take-exchange-1"
        if target is ExecutionState.CLOSING:
            updates["close_client_order_id"] = "close-client-1"
        if target is ExecutionState.CLOSED:
            updates["close_exchange_order_id"] = "close-exchange-1"
            updates["close_filled_quantity"] = Decimal("0.125")
            updates["close_average_price"] = Decimal("105")
        if target is ExecutionState.SETTLED:
            updates["realized_pnl"] = Decimal("5.5")
            updates["commission"] = Decimal("0.25")
            updates["funding"] = Decimal("-0.1")
        machine.transition(target, reason=f"advance to {target.value}", updates=updates)

        recovered = ExecutionStateMachine(EventLedger(path)).snapshot
        assert recovered is not None
        assert recovered.state is target
        assert recovered.event_count == transitions.index(target) + 2

    assert recovered.exchange_order_id == "exchange-1"
    assert recovered.filled_quantity == Decimal("0.125")
    assert recovered.close_exchange_order_id == "close-exchange-1"
    assert recovered.close_filled_quantity == Decimal("0.125")
    assert recovered.realized_pnl == Decimal("5.5")
    assert recovered.commission == Decimal("0.25")
    assert recovered.funding == Decimal("-0.1")


def test_illegal_transition_is_rejected_without_appending(tmp_path):
    ledger = EventLedger(tmp_path / "execution.jsonl")
    machine = writer_machine(ledger.path)
    machine.create(intent())

    with pytest.raises(InvalidTransition):
        machine.transition(ExecutionState.FILLED, reason="skip submission")

    assert len(ledger.load()) == 1


def test_semantically_invalid_transition_is_validated_before_append(tmp_path):
    ledger = EventLedger(tmp_path / "execution.jsonl")
    machine = writer_machine(ledger.path)
    machine.create(intent())
    machine.transition(ExecutionState.SUBMITTED, reason="submit")

    with pytest.raises(LedgerCorrupted, match="update is invalid"):
        machine.transition(
            ExecutionState.FILLED,
            reason="invalid fill",
            updates={
                "filled_quantity": Decimal("0.125"),
                "average_price": Decimal("0"),
            },
        )

    assert len(ledger.load()) == 2
    assert machine.snapshot.state is ExecutionState.SUBMITTED


def test_state_writes_require_matching_active_lease(tmp_path):
    path = tmp_path / "execution.jsonl"
    read_only = ExecutionStateMachine(EventLedger(path))

    with pytest.raises(LeaseUnavailable, match="active run lease"):
        read_only.create(intent())

    wrong_lease = RunLease(
        tmp_path / "wrong.lock",
        strategy_id="sample",
        run_id="another-run",
        owner_id="wrong-owner",
    )
    wrong_lease.acquire()
    wrong_writer = ExecutionStateMachine(EventLedger(path), lease=wrong_lease)
    with pytest.raises(LeaseUnavailable, match="ownership does not match"):
        wrong_writer.create(intent())
    wrong_lease.release()

    assert not path.exists()


def test_ledger_tampering_is_detected(tmp_path):
    path = tmp_path / "execution.jsonl"
    machine = writer_machine(path)
    machine.create(intent())
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["quantity"] = "999"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(LedgerCorrupted, match="modified"):
        EventLedger(path).load()


def test_truncated_ledger_record_is_blocking(tmp_path):
    path = tmp_path / "execution.jsonl"
    path.write_text('{"schema_version": 1', encoding="utf-8")

    with pytest.raises(LedgerCorrupted, match="invalid JSON"):
        EventLedger(path).load()
