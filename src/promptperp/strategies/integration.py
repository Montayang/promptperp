from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from promptperp.domain import OrderSide, SymbolRules
from promptperp.execution import TradeIntent
from promptperp.risk import RiskAction, RiskRequest
from promptperp.strategies.base import StrategySignal


@dataclass(frozen=True)
class StrategyExecutionPlan:
    intent: TradeIntent
    margin: Decimal
    notional: Decimal
    leverage: int
    reference_price: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal


def build_execution_plan(
    signal: StrategySignal,
    *,
    run_id: str,
    intent_id: str,
    reference_price: Decimal,
    symbol_rules: SymbolRules,
) -> StrategyExecutionPlan:
    if reference_price <= 0:
        raise ValueError("reference price must be positive")
    requested_notional = signal.margin * signal.leverage
    quantity = symbol_rules.floor_quantity(requested_notional / reference_price)
    symbol_rules.validate_notional(quantity, reference_price)
    actual_notional = quantity * reference_price
    stop = symbol_rules.floor_price(
        reference_price * (Decimal("1") - signal.stop_loss_ratio)
    )
    take = symbol_rules.floor_price(
        reference_price * (Decimal("1") + signal.take_profit_ratio)
    )
    return StrategyExecutionPlan(
        intent=TradeIntent(
            strategy_id=signal.strategy_id,
            run_id=run_id,
            intent_id=intent_id,
            symbol=signal.symbol,
            side=OrderSide.BUY,
            position_side=signal.position_side,
            quantity=quantity,
        ),
        margin=signal.margin,
        notional=actual_notional,
        leverage=signal.leverage,
        reference_price=reference_price,
        stop_loss_price=stop,
        take_profit_price=take,
    )


def build_open_risk_request(
    plan: StrategyExecutionPlan,
    *,
    available_balance: Decimal,
    market_time: datetime,
    evaluated_at: datetime,
    open_position_count: int,
    recent_order_count: int = 0,
    consecutive_failures: int = 0,
    daily_pnl: Decimal = Decimal("0"),
    order_outcome_unknown: bool = False,
    reconciliation_failed: bool = False,
    protection_incomplete: bool = False,
    foreign_objects_detected: bool = False,
) -> RiskRequest:
    return RiskRequest(
        intent=plan.intent,
        action=RiskAction.OPEN,
        market="USD_M",
        order_type="MARKET",
        margin=plan.margin,
        notional=plan.notional,
        leverage=plan.leverage,
        available_balance=available_balance,
        reference_price=plan.reference_price,
        expected_price=plan.reference_price,
        market_time=market_time,
        evaluated_at=evaluated_at,
        stop_loss_price=plan.stop_loss_price,
        open_position_count=open_position_count,
        recent_order_count=recent_order_count,
        consecutive_failures=consecutive_failures,
        daily_pnl=daily_pnl,
        order_outcome_unknown=order_outcome_unknown,
        reconciliation_failed=reconciliation_failed,
        protection_incomplete=protection_incomplete,
        foreign_objects_detected=foreign_objects_detected,
    )
