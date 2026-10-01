from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from promptperp.domain import (
    ForeignOrderDetected,
    ForeignPositionDetected,
    FuturesPosition,
    Order,
    OrderSide,
    OrderStatus,
    PositionSide,
    ProtectionClose,
    ProtectionFailed,
    ReconciliationFailed,
    RequestUnknown,
    RiskRejected,
)
from promptperp.execution import (
    EventLedger,
    ExecutionCoordinator,
    ExecutionState,
    ExecutionStateMachine,
    OwnershipReconciler,
    RunLease,
    TradeIntent,
)


def writer_machine(ledger):
    lease = RunLease(
        ledger.path.with_suffix(".lock"),
        strategy_id="sample",
        run_id="run-001",
        owner_id="test-worker",
    )
    lease.acquire()
    return ExecutionStateMachine(ledger, lease=lease)


def approval(intent, *, allowed=True, action="OPEN"):
    return SimpleNamespace(
        allowed=allowed,
        strategy_id=intent.strategy_id,
        run_id=intent.run_id,
        intent_id=intent.intent_id,
        action=action,
        policy_fingerprint="policy-hash",
    )


def make_intent() -> TradeIntent:
    return TradeIntent(
        strategy_id="sample",
        run_id="run-001",
        intent_id="intent-001",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        quantity=Decimal("2"),
    )


def make_order(
    client_order_id: str,
    *,
    status: OrderStatus,
    executed: str,
    order_id: str = "exchange-1",
) -> Order:
    return Order(
        symbol="BTCUSDT",
        order_id=order_id,
        client_order_id=client_order_id,
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        status=status,
        requested_quantity=Decimal("2"),
        executed_quantity=Decimal(executed),
        average_price=Decimal("100") if Decimal(executed) else Decimal("0"),
    )


class UncertainAcceptedGateway:
    def __init__(self):
        self.orders = {}
        self.place_calls = 0
        self.query_calls = 0

    def place_market_order(self, **kwargs):
        self.place_calls += 1
        self.orders[kwargs["client_order_id"]] = make_order(
            kwargs["client_order_id"],
            status=OrderStatus.FILLED,
            executed="2",
        )
        raise RequestUnknown("write timed out")

    def get_order(self, **kwargs):
        self.query_calls += 1
        return self.orders[kwargs["client_order_id"]]

    def cancel_order(self, **kwargs):
        raise AssertionError("recovery must not cancel orders")


class RecordingOwnershipSink:
    def __init__(self):
        self.events = []

    def begin_intent(self, intent, *, opened_at):
        self.events.append(("begin", intent.intent_id, opened_at))

    def prepare_order(self, **kwargs):
        self.events.append(("prepare", kwargs))

    def confirm_order(self, **kwargs):
        self.events.append(("confirm", kwargs))

    def confirm_algo_trigger(self, **kwargs):
        self.events.append(("trigger", kwargs))

    def close_intent(self, intent_id, *, closed_at):
        self.events.append(("close", intent_id, closed_at))


def test_write_timeout_recovers_by_client_id_without_duplicate_submission(tmp_path):
    ledger = EventLedger(tmp_path / "execution.jsonl")
    gateway = UncertainAcceptedGateway()
    coordinator = ExecutionCoordinator(
        state_machine=writer_machine(ledger),
        execution_gateway=gateway,
    )

    with pytest.raises(RequestUnknown):
        coordinator.submit_entry(make_intent(), approval(make_intent()))

    with pytest.raises(ReconciliationFailed, match="new entries are blocked"):
        OwnershipReconciler.ensure_run_can_open([coordinator.state_machine.snapshot])

    assert coordinator.state_machine.snapshot.state is ExecutionState.SUBMITTED

    coordinator.state_machine.lease.release()
    restarted = ExecutionCoordinator(
        state_machine=writer_machine(ledger),
        execution_gateway=gateway,
    )
    recovered = restarted.recover_entry()

    assert recovered.state is ExecutionState.FILLED
    assert recovered.exchange_order_id == "exchange-1"
    assert gateway.place_calls == 1
    assert gateway.query_calls == 1


class ProgressingGateway:
    def __init__(self):
        self.client_order_id = None
        self.status = OrderStatus.PARTIALLY_FILLED

    def place_market_order(self, **kwargs):
        self.client_order_id = kwargs["client_order_id"]
        executed = "2" if self.status is OrderStatus.FILLED else "0.75"
        return make_order(
            self.client_order_id,
            status=self.status,
            executed=executed,
        )

    def get_order(self, **kwargs):
        executed = "2" if self.status is OrderStatus.FILLED else "0.75"
        return make_order(
            kwargs["client_order_id"],
            status=self.status,
            executed=executed,
        )

    def cancel_order(self, **kwargs):
        raise AssertionError


class OwnershipAwareGateway(ProgressingGateway):
    def __init__(self, sink):
        super().__init__()
        self.status = OrderStatus.FILLED
        self.sink = sink

    def place_market_order(self, **kwargs):
        role = "ENTRY" if kwargs["side"] is OrderSide.BUY else "EXIT"
        assert any(
            event[0] == "prepare" and event[1]["role"] == role
            for event in self.sink.events
        )
        return super().place_market_order(**kwargs)


def test_partial_fill_blocks_until_reconciliation_reports_full_fill(tmp_path):
    gateway = ProgressingGateway()
    coordinator = ExecutionCoordinator(
        state_machine=writer_machine(EventLedger(tmp_path / "execution.jsonl")),
        execution_gateway=gateway,
    )

    partial = coordinator.submit_entry(make_intent(), approval(make_intent()))
    assert partial.state is ExecutionState.PARTIALLY_FILLED
    assert partial.filled_quantity == Decimal("0.75")

    gateway.status = OrderStatus.FILLED
    filled = coordinator.recover_entry()
    assert filled.state is ExecutionState.FILLED
    assert filled.filled_quantity == Decimal("2")


class ProtectionFake:
    def __init__(self, *, fail_after_place=False, missing_take=False, sink=None):
        self.fail_after_place = fail_after_place
        self.missing_take = missing_take
        self.sink = sink
        self.place_calls = 0
        self.ids = set()

    def place_protection(
        self, *, snapshot, stop_client_order_id, take_profit_client_order_id
    ):
        if self.sink is not None:
            prepared_roles = {
                event[1]["role"] for event in self.sink.events if event[0] == "prepare"
            }
            assert {"STOP", "TAKE_PROFIT"} <= prepared_roles
        self.place_calls += 1
        self.ids.add(stop_client_order_id)
        if not self.missing_take:
            self.ids.add(take_profit_client_order_id)
        if self.fail_after_place:
            raise TimeoutError

    def get_protection_order_id(self, *, snapshot, client_order_id, role):
        return f"algo-{client_order_id}" if client_order_id in self.ids else None

    def get_triggered_close(self, *, snapshot):
        return None

    def cancel_open_protection(self, *, snapshot):
        del snapshot


def test_denied_risk_authorization_has_no_side_effect(tmp_path):
    gateway = ProgressingGateway()
    coordinator = ExecutionCoordinator(
        state_machine=writer_machine(EventLedger(tmp_path / "execution.jsonl")),
        execution_gateway=gateway,
    )
    trade_intent = make_intent()

    with pytest.raises(RiskRejected):
        coordinator.submit_entry(trade_intent, approval(trade_intent, allowed=False))

    assert coordinator.state_machine.snapshot is None
    assert gateway.client_order_id is None


def filled_coordinator(tmp_path, *, ownership_sink=None, gateway=None):
    gateway = gateway or ProgressingGateway()
    gateway.status = OrderStatus.FILLED
    ledger = EventLedger(tmp_path / "execution.jsonl")
    coordinator = ExecutionCoordinator(
        state_machine=writer_machine(ledger),
        execution_gateway=gateway,
        ownership_sink=ownership_sink,
    )
    coordinator.submit_entry(make_intent(), approval(make_intent()))
    return ledger, gateway, coordinator


def test_ownership_is_prepared_before_entry_and_confirmed_after_acceptance(tmp_path):
    sink = RecordingOwnershipSink()
    gateway = OwnershipAwareGateway(sink)
    _, _, coordinator = filled_coordinator(
        tmp_path,
        ownership_sink=sink,
        gateway=gateway,
    )

    snapshot = coordinator.state_machine.snapshot
    assert [event[0] for event in sink.events] == ["begin", "prepare", "confirm"]
    assert sink.events[1][1]["role"] == "ENTRY"
    assert sink.events[2][1] == {
        "client_order_id": snapshot.entry_client_order_id,
        "exchange_order_id": "exchange-1",
    }


def test_protection_ownership_is_retryable_and_confirmed(tmp_path):
    sink = RecordingOwnershipSink()
    ledger, gateway, coordinator = filled_coordinator(
        tmp_path,
        ownership_sink=sink,
    )
    protection = ProtectionFake(fail_after_place=True, sink=sink)

    with pytest.raises(ProtectionFailed, match="unknown"):
        coordinator.ensure_protection(protection)

    protection.fail_after_place = False
    coordinator.state_machine.lease.release()
    restarted = ExecutionCoordinator(
        state_machine=writer_machine(ledger),
        execution_gateway=gateway,
        ownership_sink=sink,
    )
    restarted.ensure_protection(protection)

    retried_prepared = [event for event in sink.events if event[0] == "prepare"]
    assert retried_prepared[1][1]["created_at"] == retried_prepared[3][1]["created_at"]
    assert retried_prepared[2][1]["created_at"] == retried_prepared[4][1]["created_at"]
    assert [event[0] for event in sink.events].count("confirm") == 3


def test_protection_timeout_restarts_with_query_not_duplicate_place(tmp_path):
    ledger, execution_gateway, coordinator = filled_coordinator(tmp_path)
    protection = ProtectionFake(fail_after_place=True)

    with pytest.raises(ProtectionFailed, match="unknown"):
        coordinator.ensure_protection(protection)

    protection.fail_after_place = False
    coordinator.state_machine.lease.release()
    restarted = ExecutionCoordinator(
        state_machine=writer_machine(ledger),
        execution_gateway=execution_gateway,
    )
    protected = restarted.ensure_protection(protection)

    assert protected.state is ExecutionState.PROTECTED
    assert protected.stop_exchange_order_id.startswith("algo-")
    assert protected.take_profit_exchange_order_id.startswith("algo-")
    assert protection.place_calls == 1


def test_protection_requires_both_orders_to_be_confirmed(tmp_path):
    _, _, coordinator = filled_coordinator(tmp_path)
    protection = ProtectionFake(missing_take=True)

    with pytest.raises(ProtectionFailed, match="both"):
        coordinator.ensure_protection(protection)

    assert coordinator.state_machine.snapshot.state is ExecutionState.PROTECTING


def test_mismatched_exchange_order_is_not_adopted(tmp_path):
    gateway = ProgressingGateway()
    coordinator = ExecutionCoordinator(
        state_machine=writer_machine(EventLedger(tmp_path / "execution.jsonl")),
        execution_gateway=gateway,
    )
    coordinator.state_machine.create(make_intent())
    coordinator.state_machine.transition(
        ExecutionState.SUBMITTED,
        reason="test unknown submission",
    )
    foreign = make_order(
        "foreign-client-id",
        status=OrderStatus.FILLED,
        executed="2",
    )
    gateway.get_order = lambda **kwargs: foreign

    with pytest.raises(ReconciliationFailed, match="ownership"):
        coordinator.recover_entry()


def test_foreign_orders_and_positions_are_blocking(tmp_path):
    _, _, coordinator = filled_coordinator(tmp_path)
    snapshot = coordinator.state_machine.snapshot
    foreign_order = make_order(
        "another-run",
        status=OrderStatus.NEW,
        executed="0",
    )

    with pytest.raises(ForeignOrderDetected):
        OwnershipReconciler.verify_orders([snapshot], [foreign_order])

    foreign_position = FuturesPosition(
        symbol="ETHUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
    )
    with pytest.raises(ForeignPositionDetected):
        OwnershipReconciler.verify_positions([snapshot], [foreign_position])


def test_position_ownership_aggregates_multiple_intents(tmp_path):
    _, _, coordinator = filled_coordinator(tmp_path)
    first = coordinator.state_machine.snapshot
    second = replace(
        first,
        intent=replace(first.intent, intent_id="intent-002"),
        filled_quantity=Decimal("1"),
    )
    combined = FuturesPosition(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("3"),
        entry_price=Decimal("100"),
    )

    OwnershipReconciler.verify_positions([first, second], [combined])


def close_order(
    client_order_id: str,
    *,
    status: OrderStatus,
    executed: str,
    order_id: str = "close-exchange-1",
) -> Order:
    return Order(
        symbol="BTCUSDT",
        order_id=order_id,
        client_order_id=client_order_id,
        side=OrderSide.SELL,
        position_side=PositionSide.LONG,
        status=status,
        requested_quantity=Decimal("2"),
        executed_quantity=Decimal(executed),
        average_price=Decimal("105") if Decimal(executed) else Decimal("0"),
    )


class LifecycleGateway(ProgressingGateway):
    def __init__(self):
        super().__init__()
        self.status = OrderStatus.FILLED
        self.close_status = OrderStatus.FILLED
        self.close_unknown = False
        self.close_place_calls = 0
        self.orders = {}

    def place_market_order(self, **kwargs):
        client_id = kwargs["client_order_id"]
        if kwargs["side"] is OrderSide.BUY:
            order = make_order(
                client_id,
                status=OrderStatus.FILLED,
                executed="2",
            )
        else:
            self.close_place_calls += 1
            executed = "2" if self.close_status is OrderStatus.FILLED else "0.75"
            order = close_order(
                client_id,
                status=self.close_status,
                executed=executed,
            )
        self.orders[client_id] = order
        if kwargs["side"] is OrderSide.SELL and self.close_unknown:
            raise RequestUnknown("close write timed out")
        return order

    def get_order(self, **kwargs):
        client_id = kwargs["client_order_id"]
        order = self.orders[client_id]
        if order.side is OrderSide.SELL:
            executed = "2" if self.close_status is OrderStatus.FILLED else "0.75"
            order = close_order(
                client_id,
                status=self.close_status,
                executed=executed,
            )
            self.orders[client_id] = order
        return order


def protected_lifecycle(tmp_path, gateway, *, ownership_sink=None):
    ledger = EventLedger(tmp_path / "execution.jsonl")
    coordinator = ExecutionCoordinator(
        state_machine=writer_machine(ledger),
        execution_gateway=gateway,
        ownership_sink=ownership_sink,
    )
    trade_intent = make_intent()
    coordinator.submit_entry(trade_intent, approval(trade_intent))
    coordinator.ensure_protection(ProtectionFake())
    return ledger, coordinator


def test_close_write_timeout_recovers_without_duplicate_reduce_order(tmp_path):
    gateway = LifecycleGateway()
    gateway.close_unknown = True
    ledger, coordinator = protected_lifecycle(tmp_path, gateway)
    trade_intent = make_intent()

    with pytest.raises(RequestUnknown, match="timed out"):
        coordinator.submit_close(
            approval(trade_intent, action="REDUCE"),
            reason="strategy rotation",
        )

    assert coordinator.state_machine.snapshot.state is ExecutionState.CLOSING
    assert gateway.close_place_calls == 1

    gateway.close_unknown = False
    coordinator.state_machine.lease.release()
    restarted = ExecutionCoordinator(
        state_machine=writer_machine(ledger),
        execution_gateway=gateway,
    )
    closed = restarted.recover_close()

    assert closed.state is ExecutionState.CLOSED
    assert closed.close_filled_quantity == Decimal("2")
    assert gateway.close_place_calls == 1


def test_close_ownership_is_prepared_before_exchange_and_confirmed(tmp_path):
    sink = RecordingOwnershipSink()
    gateway = LifecycleGateway()
    _, coordinator = protected_lifecycle(
        tmp_path,
        gateway,
        ownership_sink=sink,
    )

    closed = coordinator.submit_close(
        approval(make_intent(), action="REDUCE"),
        reason="strategy rotation",
    )

    exit_prepare = [
        event
        for event in sink.events
        if event[0] == "prepare" and event[1]["role"] == "EXIT"
    ]
    assert len(exit_prepare) == 1
    assert (
        "confirm",
        {
            "client_order_id": closed.close_client_order_id,
            "exchange_order_id": "close-exchange-1",
        },
    ) in sink.events


def test_partial_close_tracks_remaining_owned_quantity_until_recovered(tmp_path):
    gateway = LifecycleGateway()
    gateway.close_status = OrderStatus.PARTIALLY_FILLED
    _, coordinator = protected_lifecycle(tmp_path, gateway)
    trade_intent = make_intent()

    partial = coordinator.submit_close(
        approval(trade_intent, action="REDUCE"),
        reason="strategy rotation",
    )

    assert partial.state is ExecutionState.CLOSING
    assert partial.close_filled_quantity == Decimal("0.75")
    assert partial.owned_quantity == Decimal("1.25")
    OwnershipReconciler.verify_positions(
        [partial],
        [
            FuturesPosition(
                symbol="BTCUSDT",
                side=PositionSide.LONG,
                quantity=Decimal("1.25"),
                entry_price=Decimal("100"),
            )
        ],
    )

    gateway.close_status = OrderStatus.FILLED
    closed = coordinator.recover_close()
    assert closed.state is ExecutionState.CLOSED
    assert not closed.owns_position
    assert gateway.close_place_calls == 1


def test_close_requires_matching_reduce_authorization(tmp_path):
    gateway = LifecycleGateway()
    _, coordinator = protected_lifecycle(tmp_path, gateway)
    trade_intent = make_intent()

    with pytest.raises(RiskRejected, match="not for reduce"):
        coordinator.submit_close(
            approval(trade_intent, action="OPEN"),
            reason="strategy rotation",
        )

    assert coordinator.state_machine.snapshot.state is ExecutionState.PROTECTED
    assert gateway.close_place_calls == 0


def test_settlement_requires_closed_execution_and_replays(tmp_path):
    gateway = LifecycleGateway()
    ledger, coordinator = protected_lifecycle(tmp_path, gateway)
    trade_intent = make_intent()

    with pytest.raises(ReconciliationFailed, match="not ready"):
        coordinator.settle(
            realized_pnl=Decimal("10"),
            commission=Decimal("0.5"),
            funding=Decimal("-0.1"),
        )

    coordinator.submit_close(
        approval(trade_intent, action="REDUCE"),
        reason="strategy rotation",
    )
    with pytest.raises(ReconciliationFailed, match="cleanup"):
        coordinator.settle(
            realized_pnl=Decimal("10"),
            commission=Decimal("0.5"),
            funding=Decimal("-0.1"),
        )
    coordinator.cleanup_protection(ProtectionFake())
    settled = coordinator.settle(
        realized_pnl=Decimal("10"),
        commission=Decimal("0.5"),
        funding=Decimal("-0.1"),
    )

    assert settled.state is ExecutionState.SETTLED
    assert settled.realized_pnl == Decimal("10")
    assert settled.commission == Decimal("0.5")
    assert settled.funding == Decimal("-0.1")
    replayed = ExecutionStateMachine(ledger).snapshot
    assert replayed == settled


class TriggeredProtectionFake(ProtectionFake):
    def __init__(self, triggered):
        super().__init__()
        self.triggered = triggered

    def get_triggered_close(self, *, snapshot):
        return self.triggered


def test_owned_take_profit_trigger_advances_to_closed_without_market_resubmit(
    tmp_path,
):
    gateway = LifecycleGateway()
    _, coordinator = protected_lifecycle(tmp_path, gateway)
    snapshot = coordinator.state_machine.snapshot
    triggered_order = close_order(
        "exchange-generated-close-client",
        status=OrderStatus.FILLED,
        executed="2",
        order_id="triggered-order-42",
    )
    protection = TriggeredProtectionFake(
        ProtectionClose(
            protection_client_order_id=snapshot.take_profit_client_order_id,
            order=triggered_order,
        )
    )

    closed = coordinator.recover_protection_close(protection)

    assert closed.state is ExecutionState.CLOSED
    assert closed.close_client_order_id == snapshot.take_profit_client_order_id
    assert closed.close_exchange_order_id == "triggered-order-42"
    assert closed.close_average_price == Decimal("105")
    assert gateway.close_place_calls == 0


def test_protection_trigger_confirms_actual_exchange_order_ownership(tmp_path):
    sink = RecordingOwnershipSink()
    gateway = LifecycleGateway()
    _, coordinator = protected_lifecycle(
        tmp_path,
        gateway,
        ownership_sink=sink,
    )
    snapshot = coordinator.state_machine.snapshot
    protection = TriggeredProtectionFake(
        ProtectionClose(
            protection_client_order_id=snapshot.stop_client_order_id,
            order=close_order(
                "exchange-generated-close-client",
                status=OrderStatus.FILLED,
                executed="2",
                order_id="triggered-order-99",
            ),
        )
    )

    coordinator.recover_protection_close(protection)

    assert (
        "trigger",
        {
            "client_order_id": snapshot.stop_client_order_id,
            "exchange_order_id": "triggered-order-99",
        },
    ) in sink.events


def test_foreign_or_incomplete_protection_trigger_is_blocking(tmp_path):
    gateway = LifecycleGateway()
    _, coordinator = protected_lifecycle(tmp_path, gateway)
    foreign = TriggeredProtectionFake(
        ProtectionClose(
            protection_client_order_id="foreign-protection",
            order=close_order(
                "exchange-generated-close-client",
                status=OrderStatus.FILLED,
                executed="2",
            ),
        )
    )

    with pytest.raises(ReconciliationFailed, match="not owned"):
        coordinator.recover_protection_close(foreign)

    assert coordinator.state_machine.snapshot.state is ExecutionState.PROTECTED

    missing = TriggeredProtectionFake(None)
    with pytest.raises(ReconciliationFailed, match="no owned"):
        coordinator.recover_protection_close(missing)

    assert coordinator.state_machine.snapshot.state is ExecutionState.PROTECTED
