from __future__ import annotations

from decimal import Decimal

import pytest

from promptperp.domain import PositionSide
from promptperp.execution.basket import (
    BasketAdjustment,
    BasketAdjustmentRole,
    BasketRebalancePlan,
)
from promptperp.execution.basket_state import (
    BasketExecutionError,
    BasketExecutionStateMachine,
    BasketLegState,
    BasketPhase,
)
from promptperp.execution.lease import LeaseUnavailable, RunLease
from promptperp.execution.ledger import EventLedger, LedgerCorrupted

STRATEGY = "strategy_beta"
RUN = "strategy_beta-run-001"
BASKET = "basket-001"
FINGERPRINT = "a" * 64


def _plan() -> BasketRebalancePlan:
    return BasketRebalancePlan(
        reductions=(
            BasketAdjustment(
                role=BasketAdjustmentRole.REDUCE,
                symbol="OLDUSDT",
                position_side=PositionSide.LONG,
                quantity=Decimal("1"),
                target_quantity=Decimal("0"),
            ),
        ),
        increases=(
            BasketAdjustment(
                role=BasketAdjustmentRole.INCREASE,
                symbol="NEWUSDT",
                position_side=PositionSide.SHORT,
                quantity=Decimal("2"),
                target_quantity=Decimal("2"),
            ),
        ),
    )


def _writer(tmp_path):
    ledger = EventLedger(tmp_path / "basket.jsonl")
    lease = RunLease(
        tmp_path / "basket.lock",
        strategy_id=STRATEGY,
        run_id=RUN,
        owner_id="test-worker",
    )
    lease.acquire()
    return BasketExecutionStateMachine(ledger, lease=lease), ledger, lease


def _create(machine, plan=None):
    return machine.create(
        strategy_id=STRATEGY,
        run_id=RUN,
        basket_id=BASKET,
        target_fingerprint=FINGERPRINT,
        plan=_plan() if plan is None else plan,
    )


def test_reductions_complete_before_any_increase_and_recover_mid_fill(tmp_path) -> None:
    machine, ledger, lease = _writer(tmp_path)
    created = _create(machine)
    assert created.phase is BasketPhase.PREPARED
    reduction_id = created.legs[0].client_order_id
    assert created.legs[1].client_order_id != reduction_id

    machine.start()
    reduction = machine.next_leg()
    assert reduction is not None
    assert reduction.adjustment.role is BasketAdjustmentRole.REDUCE
    machine.mark_submitted(reduction.index)
    machine.confirm(
        reduction.index,
        exchange_order_id="order-reduce",
        cumulative_filled=Decimal("0.4"),
        average_price=Decimal("100"),
    )

    recovered = BasketExecutionStateMachine(ledger)
    pending = recovered.next_leg()
    assert pending is not None
    assert pending.client_order_id == reduction_id
    assert pending.state is BasketLegState.PARTIALLY_FILLED

    machine.confirm(
        reduction.index,
        exchange_order_id="order-reduce",
        cumulative_filled=Decimal("1"),
        average_price=Decimal("101"),
    )
    assert machine.snapshot is not None
    assert machine.snapshot.phase is BasketPhase.INCREASING
    assert machine.next_leg() is not None
    assert machine.next_leg().adjustment.role is BasketAdjustmentRole.INCREASE
    lease.release()


def test_increase_failure_stops_new_risk_until_external_flat_reconciliation(
    tmp_path,
) -> None:
    machine, _, lease = _writer(tmp_path)
    _create(
        machine,
        BasketRebalancePlan(reductions=(), increases=_plan().increases),
    )
    machine.start()
    increase = machine.next_leg()
    assert increase is not None
    machine.mark_submitted(increase.index)
    machine.confirm(
        increase.index,
        exchange_order_id="order-increase",
        cumulative_filled=Decimal("0.5"),
        average_price=Decimal("50"),
    )
    machine.fail_leg(increase.index, reason="exchange rejected remainder")

    assert machine.snapshot is not None
    assert machine.snapshot.phase is BasketPhase.ROLLING_BACK
    assert machine.next_leg() is None
    with pytest.raises(BasketExecutionError, match="not flat"):
        machine.confirm_safe_flat({("NEWUSDT", PositionSide.SHORT): Decimal("0.5")})
    machine.confirm_safe_flat({})
    assert machine.snapshot.phase is BasketPhase.SAFE_FLAT
    lease.release()


def test_reduction_failure_blocks_without_opening_new_risk(tmp_path) -> None:
    machine, _, lease = _writer(tmp_path)
    _create(machine)
    machine.start()
    reduction = machine.next_leg()
    assert reduction is not None
    machine.mark_submitted(reduction.index)
    machine.fail_leg(reduction.index, reason="reduction order rejected")

    assert machine.snapshot is not None
    assert machine.snapshot.phase is BasketPhase.BLOCKED
    assert machine.next_leg() is None
    assert machine.snapshot.legs[1].state is BasketLegState.PENDING
    lease.release()


def test_empty_rebalance_completes_without_orders(tmp_path) -> None:
    machine, _, lease = _writer(tmp_path)
    _create(machine, BasketRebalancePlan(reductions=(), increases=()))
    result = machine.start()
    assert result.phase is BasketPhase.COMPLETE
    assert result.legs == ()
    lease.release()


def test_resume_advances_after_crash_between_final_fill_and_phase_event(
    tmp_path,
) -> None:
    machine, ledger, lease = _writer(tmp_path)
    _create(
        machine,
        BasketRebalancePlan(reductions=_plan().reductions, increases=()),
    )
    machine.start()
    reduction = machine.next_leg()
    assert reduction is not None
    machine.mark_submitted(reduction.index)
    ledger.append(
        strategy_id=STRATEGY,
        run_id=RUN,
        intent_id=BASKET,
        event_type="BASKET_LEG",
        payload={
            "index": reduction.index,
            "from": BasketLegState.SUBMITTED.value,
            "to": BasketLegState.FILLED.value,
            "reason": "exchange order reconciled",
            "exchange_order_id": "order-reduce",
            "filled_quantity": "1",
            "average_price": "100",
        },
    )

    recovered = BasketExecutionStateMachine(ledger, lease=lease)
    assert recovered.snapshot is not None
    assert recovered.snapshot.phase is BasketPhase.REDUCING
    assert recovered.next_leg() is None
    assert recovered.resume().phase is BasketPhase.COMPLETE
    lease.release()


def test_basket_writes_require_matching_lease_and_illegal_history_fails(
    tmp_path,
) -> None:
    ledger = EventLedger(tmp_path / "basket.jsonl")
    read_only = BasketExecutionStateMachine(ledger)
    with pytest.raises(LeaseUnavailable, match="active run lease"):
        _create(read_only)

    machine, ledger, lease = _writer(tmp_path)
    _create(machine)
    ledger.append(
        strategy_id=STRATEGY,
        run_id=RUN,
        intent_id=BASKET,
        event_type="BASKET_PHASE",
        payload={
            "from": BasketPhase.PREPARED.value,
            "to": BasketPhase.ROLLING_BACK.value,
            "reason": "illegal skip",
        },
    )
    with pytest.raises(LedgerCorrupted, match="required evidence"):
        BasketExecutionStateMachine(ledger)
    lease.release()
