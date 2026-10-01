from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from promptperp.domain import OrderSide, PositionSide, RiskRejected
from promptperp.execution import ExecutionCoordinator, TradeIntent
from promptperp.risk import (
    MemoryRiskAuditSink,
    OperatingMode,
    RiskAction,
    RiskEngine,
    RiskPolicy,
    RiskRequest,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def policy(**overrides) -> RiskPolicy:
    values = {
        "policy_id": "operator-default",
        "version": 1,
        "mode": OperatingMode.NORMAL,
        "allowed_markets": frozenset({"USD_M"}),
        "allowed_symbols": frozenset({"BTCUSDT", "ETHUSDT"}),
        "allowed_sides": frozenset({"BUY", "SELL"}),
        "allowed_order_types": frozenset({"MARKET"}),
        "max_margin": Decimal("400"),
        "max_notional": Decimal("2000"),
        "max_leverage": 5,
        "max_open_positions": 5,
        "balance_buffer": Decimal("50"),
        "require_stop_loss": True,
        "max_loss_per_trade": Decimal("30"),
        "max_market_age_seconds": Decimal("5"),
        "max_price_deviation_bps": Decimal("50"),
        "max_orders_per_window": 4,
        "max_consecutive_failures": 3,
        "daily_loss_limit": Decimal("100"),
    }
    values.update(overrides)
    return RiskPolicy(**values)


def request(**overrides) -> RiskRequest:
    intent = overrides.pop(
        "intent",
        TradeIntent(
            strategy_id="sample",
            run_id="run-001",
            intent_id="intent-001",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            position_side=PositionSide.LONG,
            quantity=Decimal("1"),
        ),
    )
    values = {
        "intent": intent,
        "action": RiskAction.OPEN,
        "market": "USD_M",
        "order_type": "MARKET",
        "margin": Decimal("200"),
        "notional": Decimal("1000"),
        "leverage": 5,
        "available_balance": Decimal("500"),
        "reference_price": Decimal("100"),
        "expected_price": Decimal("100.25"),
        "market_time": NOW - timedelta(seconds=1),
        "evaluated_at": NOW,
        "stop_loss_price": Decimal("90"),
        "open_position_count": 1,
        "recent_order_count": 1,
        "consecutive_failures": 0,
        "daily_pnl": Decimal("-10"),
    }
    values.update(overrides)
    return RiskRequest(**values)


def evaluate(value: RiskRequest, selected_policy: RiskPolicy | None = None):
    sink = MemoryRiskAuditSink()
    decision = RiskEngine(selected_policy or policy(), sink).evaluate(value)
    assert sink.decisions == [decision]
    return decision


def test_valid_open_is_allowed_and_audited_without_account_values():
    decision = evaluate(request())

    assert decision.allowed is True
    assert decision.reason_codes == ("ALLOWED",)
    assert decision.policy_fingerprint == policy().fingerprint
    assert "available_balance" not in decision.context
    assert "api" not in str(decision).lower()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"market": "COIN_M"}, "MARKET_NOT_ALLOWED"),
        (
            {
                "intent": replace(
                    request().intent,
                    symbol="XRPUSDT",
                )
            },
            "SYMBOL_NOT_ALLOWED",
        ),
        ({"order_type": "LIMIT"}, "ORDER_TYPE_NOT_ALLOWED"),
        ({"margin": Decimal("400.01")}, "MARGIN_LIMIT_EXCEEDED"),
        ({"notional": Decimal("2000.01")}, "NOTIONAL_LIMIT_EXCEEDED"),
        ({"leverage": 6}, "LEVERAGE_LIMIT_EXCEEDED"),
        ({"open_position_count": 5}, "POSITION_COUNT_LIMIT_REACHED"),
        (
            {"available_balance": Decimal("249.99")},
            "BALANCE_BUFFER_BREACHED",
        ),
        ({"recent_order_count": 4}, "ORDER_RATE_LIMIT_REACHED"),
        ({"consecutive_failures": 3}, "FAILURE_THRESHOLD_REACHED"),
        ({"daily_pnl": Decimal("-100")}, "DAILY_LOSS_LIMIT_REACHED"),
        (
            {"market_time": NOW - timedelta(seconds=5, microseconds=1)},
            "MARKET_DATA_STALE",
        ),
        ({"expected_price": Decimal("100.51")}, "PRICE_DEVIATION_EXCEEDED"),
        ({"stop_loss_price": None}, "STOP_LOSS_REQUIRED"),
        ({"market_time": NOW + timedelta(microseconds=1)}, "MARKET_DATA_STALE"),
        ({"stop_loss_price": Decimal("101")}, "STOP_LOSS_DIRECTION_INVALID"),
        ({"stop_loss_price": Decimal("60")}, "MAX_LOSS_EXCEEDED"),
        ({"order_outcome_unknown": True}, "ORDER_OUTCOME_UNKNOWN"),
        ({"reconciliation_failed": True}, "RECONCILIATION_FAILED"),
        ({"protection_incomplete": True}, "PROTECTION_INCOMPLETE"),
        ({"foreign_objects_detected": True}, "FOREIGN_OBJECTS_DETECTED"),
    ],
)
def test_each_risk_limit_rejects_with_stable_reason(change, reason):
    decision = evaluate(request(**change))

    assert decision.allowed is False
    assert reason in decision.reason_codes


def test_exact_limits_are_inclusive():
    decision = evaluate(
        request(
            margin=Decimal("400"),
            notional=Decimal("2000"),
            leverage=5,
            available_balance=Decimal("450"),
            market_time=NOW - timedelta(seconds=5),
            expected_price=Decimal("100.5"),
            stop_loss_price=Decimal("70.5"),
            open_position_count=4,
            recent_order_count=3,
            consecutive_failures=2,
            daily_pnl=Decimal("-99.99"),
        )
    )

    assert decision.allowed is True


@pytest.mark.parametrize("mode", [OperatingMode.REDUCE_ONLY, OperatingMode.KILL_SWITCH])
def test_degraded_modes_block_open_but_allow_exact_owned_reduction(mode):
    selected = policy(mode=mode)
    opened = evaluate(request(), selected)
    reduced = evaluate(
        request(
            action=RiskAction.REDUCE,
            margin=Decimal("0"),
            owned_position_quantity=Decimal("1"),
            ownership_proven=True,
            stop_loss_price=None,
        ),
        selected,
    )

    assert opened.allowed is False
    assert reduced.allowed is True


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"ownership_proven": False}, "REDUCTION_OWNERSHIP_UNPROVEN"),
        ({"owned_position_quantity": Decimal("0")}, "NO_OWNED_POSITION"),
        (
            {"owned_position_quantity": Decimal("0.5")},
            "REDUCTION_EXCEEDS_OWNED_POSITION",
        ),
        ({"order_outcome_unknown": True}, "ORDER_OUTCOME_UNKNOWN"),
    ],
)
def test_reduction_must_be_exactly_owned_and_account_state_known(change, reason):
    values = {
        "action": RiskAction.REDUCE,
        "margin": Decimal("0"),
        "owned_position_quantity": Decimal("1"),
        "ownership_proven": True,
        "stop_loss_price": None,
    }
    values.update(change)

    decision = evaluate(request(**values), policy(mode=OperatingMode.KILL_SWITCH))

    assert decision.allowed is False
    assert reason in decision.reason_codes


def test_combined_failures_are_all_audited():
    decision = evaluate(
        request(
            margin=Decimal("500"),
            leverage=10,
            order_outcome_unknown=True,
            foreign_objects_detected=True,
        )
    )

    assert decision.reason_codes == (
        "ORDER_OUTCOME_UNKNOWN",
        "FOREIGN_OBJECTS_DETECTED",
        "MARGIN_LIMIT_EXCEEDED",
        "LEVERAGE_LIMIT_EXCEEDED",
        "BALANCE_BUFFER_BREACHED",
    )


def test_real_risk_decision_is_consumed_by_execution_gate():
    trade_request = request()
    allowed = evaluate(trade_request)
    ExecutionCoordinator._require_open_authorization(trade_request.intent, allowed)

    denied = evaluate(request(margin=Decimal("401")))
    with pytest.raises(RiskRejected):
        ExecutionCoordinator._require_open_authorization(trade_request.intent, denied)


def test_policy_is_frozen_versioned_and_fingerprint_changes():
    original = policy()

    with pytest.raises(FrozenInstanceError):
        original.max_margin = Decimal("999")

    changed = policy(version=2, max_margin=Decimal("401"))
    assert changed.fingerprint != original.fingerprint


def test_dense_margin_boundary_never_allows_values_above_limit():
    engine = RiskEngine(policy(), MemoryRiskAuditSink())

    for cents in range(39990, 40011):
        margin = Decimal(cents) / Decimal("100")
        decision = engine.evaluate(request(margin=margin))
        assert decision.allowed is (margin <= Decimal("400"))
