from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from promptperp.domain import (
    Order,
    OrderSide,
    OrderStatus,
    PositionSide,
    ReconciliationFailed,
    RequestUnknown,
    RiskRejected,
)
from promptperp.execution.basket import (
    BasketAdjustment,
    BasketAdjustmentRole,
    BasketRebalancePlan,
)
from promptperp.execution.basket_coordinator import BasketExecutionCoordinator
from promptperp.execution.basket_state import (
    BasketExecutionStateMachine,
    BasketLegState,
    BasketPhase,
)
from promptperp.execution.lease import RunLease
from promptperp.execution.ledger import EventLedger
from promptperp.execution.virtual_routes import (
    VirtualFillRole,
    VirtualFillRouteRegistry,
)

STRATEGY = "strategy_beta"
RUN = "run-1"
BASKET = "basket-1"
FINGERPRINT = "b" * 64


@dataclass(frozen=True)
class Authorization:
    allowed: bool = True
    strategy_id: str = STRATEGY
    run_id: str = RUN
    basket_id: str = BASKET
    target_fingerprint: str = FINGERPRINT


class Gateway:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.placed = []
        self.queries = []

    def place_market_order(self, **kwargs):
        self.placed.append(kwargs)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def get_order(self, **kwargs):
        self.queries.append(kwargs)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def cancel_order(self, **kwargs):
        raise AssertionError(kwargs)


def _adjustment(role, symbol, side, quantity="1"):
    target = "0" if role is BasketAdjustmentRole.REDUCE else quantity
    return BasketAdjustment(
        role=role,
        symbol=symbol,
        position_side=side,
        quantity=Decimal(quantity),
        target_quantity=Decimal(target),
    )


def _machine(tmp_path, plan):
    lease = RunLease(
        tmp_path / "run.lock",
        strategy_id=STRATEGY,
        run_id=RUN,
        owner_id="test-worker",
    )
    lease.acquire()
    machine = BasketExecutionStateMachine(
        EventLedger(tmp_path / "basket.jsonl"), lease=lease
    )
    machine.create(
        strategy_id=STRATEGY,
        run_id=RUN,
        basket_id=BASKET,
        target_fingerprint=FINGERPRINT,
        plan=plan,
    )
    return machine, lease


def _routes(tmp_path):
    registry = VirtualFillRouteRegistry(tmp_path / "routes.sqlite3")
    registry.initialize()
    return registry


def _order(
    *,
    client_id,
    symbol,
    side,
    position_side,
    status=OrderStatus.FILLED,
    executed="1",
    average="100",
):
    return Order(
        symbol=symbol,
        order_id=f"exchange-{client_id}",
        client_order_id=client_id,
        side=side,
        position_side=position_side,
        status=status,
        requested_quantity=Decimal("1"),
        executed_quantity=Decimal(executed),
        average_price=Decimal(average),
    )


def test_coordinator_executes_reduction_before_increase(tmp_path) -> None:
    plan = BasketRebalancePlan(
        reductions=(
            _adjustment(
                BasketAdjustmentRole.REDUCE,
                "OLDUSDT",
                PositionSide.LONG,
            ),
        ),
        increases=(
            _adjustment(
                BasketAdjustmentRole.INCREASE,
                "NEWUSDT",
                PositionSide.SHORT,
            ),
        ),
    )
    machine, lease = _machine(tmp_path, plan)
    reduce_id, increase_id = [leg.client_order_id for leg in machine.snapshot.legs]
    gateway = Gateway(
        [
            _order(
                client_id=reduce_id,
                symbol="OLDUSDT",
                side=OrderSide.SELL,
                position_side=PositionSide.LONG,
            ),
            _order(
                client_id=increase_id,
                symbol="NEWUSDT",
                side=OrderSide.SELL,
                position_side=PositionSide.SHORT,
            ),
        ]
    )
    routes = _routes(tmp_path)
    coordinator = BasketExecutionCoordinator(
        state_machine=machine,
        execution_gateway=gateway,
        fill_route_sink=routes,
    )

    assert coordinator.execute_next(Authorization()).phase is BasketPhase.INCREASING
    assert coordinator.execute_next(Authorization()).phase is BasketPhase.COMPLETE
    assert [call["symbol"] for call in gateway.placed] == ["OLDUSDT", "NEWUSDT"]
    assert (
        routes.route_for_order(f"exchange-{reduce_id}").role is VirtualFillRole.REDUCE
    )
    assert (
        routes.route_for_order(f"exchange-{increase_id}").role is VirtualFillRole.OPEN
    )
    lease.release()


def test_unknown_submission_recovers_by_same_client_id_without_resubmit(
    tmp_path,
) -> None:
    plan = BasketRebalancePlan(
        reductions=(),
        increases=(
            _adjustment(
                BasketAdjustmentRole.INCREASE,
                "BTCUSDT",
                PositionSide.LONG,
            ),
        ),
    )
    machine, lease = _machine(tmp_path, plan)
    client_id = machine.snapshot.legs[0].client_order_id
    recovered_order = _order(
        client_id=client_id,
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
    )
    gateway = Gateway([RequestUnknown("timeout"), recovered_order])
    coordinator = BasketExecutionCoordinator(
        state_machine=machine,
        execution_gateway=gateway,
        fill_route_sink=_routes(tmp_path),
    )

    with pytest.raises(RequestUnknown):
        coordinator.execute_next(Authorization())
    assert machine.snapshot.legs[0].state is BasketLegState.SUBMITTED
    assert coordinator.execute_next(Authorization()).phase is BasketPhase.COMPLETE
    assert len(gateway.placed) == 1
    assert gateway.queries == [{"symbol": "BTCUSDT", "client_order_id": client_id}]
    lease.release()


def test_partial_canceled_increase_enters_rollback_and_blocks_more_orders(
    tmp_path,
) -> None:
    plan = BasketRebalancePlan(
        reductions=(),
        increases=(
            _adjustment(
                BasketAdjustmentRole.INCREASE,
                "BTCUSDT",
                PositionSide.LONG,
            ),
            _adjustment(
                BasketAdjustmentRole.INCREASE,
                "ETHUSDT",
                PositionSide.LONG,
            ),
        ),
    )
    machine, lease = _machine(tmp_path, plan)
    client_id = machine.snapshot.legs[0].client_order_id
    canceled = _order(
        client_id=client_id,
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        status=OrderStatus.CANCELED,
        executed="0.4",
        average="100",
    )
    gateway = Gateway([canceled])
    coordinator = BasketExecutionCoordinator(
        state_machine=machine,
        execution_gateway=gateway,
        fill_route_sink=_routes(tmp_path),
    )

    assert coordinator.execute_next(Authorization()).phase is BasketPhase.ROLLING_BACK
    assert machine.snapshot.legs[0].filled_quantity == Decimal("0.4")
    assert machine.snapshot.legs[1].state is BasketLegState.PENDING
    with pytest.raises(ReconciliationFailed, match="flatten"):
        coordinator.execute_next(Authorization())
    assert len(gateway.placed) == 1
    lease.release()


def test_mismatched_order_and_unbound_authorization_fail_closed(tmp_path) -> None:
    plan = BasketRebalancePlan(
        reductions=(),
        increases=(
            _adjustment(
                BasketAdjustmentRole.INCREASE,
                "BTCUSDT",
                PositionSide.LONG,
            ),
        ),
    )
    machine, lease = _machine(tmp_path, plan)
    client_id = machine.snapshot.legs[0].client_order_id
    wrong = _order(
        client_id=client_id,
        symbol="ETHUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
    )
    gateway = Gateway([wrong])
    coordinator = BasketExecutionCoordinator(
        state_machine=machine,
        execution_gateway=gateway,
        fill_route_sink=_routes(tmp_path),
    )

    with pytest.raises(RiskRejected, match="does not bind"):
        coordinator.execute_next(Authorization(target_fingerprint="c" * 64))
    with pytest.raises(ReconciliationFailed, match="does not match"):
        coordinator.execute_next(Authorization())
    lease.release()
