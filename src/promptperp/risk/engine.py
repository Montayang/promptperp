from __future__ import annotations

from decimal import Decimal

from promptperp.domain import PositionSide
from promptperp.risk.models import (
    OperatingMode,
    RiskAction,
    RiskAuditSink,
    RiskDecision,
    RiskPolicy,
    RiskRequest,
)


class RiskEngine:
    """Evaluate immutable operator policy and emit an audit decision every time."""

    def __init__(self, policy: RiskPolicy, audit_sink: RiskAuditSink):
        self._policy = policy
        self._audit_sink = audit_sink

    @property
    def policy(self) -> RiskPolicy:
        return self._policy

    def evaluate(self, request: RiskRequest) -> RiskDecision:
        reasons: list[str] = []
        self._blocking_health_checks(request, reasons)
        if request.action is RiskAction.REDUCE:
            self._reduction_checks(request, reasons)
        else:
            self._opening_checks(request, reasons)
        decision = RiskDecision(
            allowed=not reasons,
            policy_id=self._policy.policy_id,
            policy_version=self._policy.version,
            policy_fingerprint=self._policy.fingerprint,
            strategy_id=request.intent.strategy_id,
            run_id=request.intent.run_id,
            intent_id=request.intent.intent_id,
            action=request.action,
            reason_codes=tuple(reasons) if reasons else ("ALLOWED",),
            decided_at=request.evaluated_at,
            context={
                "symbol": request.intent.symbol,
                "side": request.intent.side.value,
                "market": request.market.upper(),
                "order_type": request.order_type.upper(),
            },
        )
        self._audit_sink.record(decision)
        return decision

    @staticmethod
    def require_allowed(decision: RiskDecision) -> None:
        if not decision.allowed:
            raise PermissionError(
                "risk policy rejected intent: " + ",".join(decision.reason_codes)
            )

    def _blocking_health_checks(self, request: RiskRequest, reasons: list[str]) -> None:
        if request.order_outcome_unknown:
            reasons.append("ORDER_OUTCOME_UNKNOWN")
        if request.reconciliation_failed:
            reasons.append("RECONCILIATION_FAILED")
        if request.protection_incomplete:
            reasons.append("PROTECTION_INCOMPLETE")
        if request.foreign_objects_detected:
            reasons.append("FOREIGN_OBJECTS_DETECTED")

    def _reduction_checks(self, request: RiskRequest, reasons: list[str]) -> None:
        if not request.ownership_proven:
            reasons.append("REDUCTION_OWNERSHIP_UNPROVEN")
        if request.owned_position_quantity <= 0:
            reasons.append("NO_OWNED_POSITION")
        if request.intent.quantity > request.owned_position_quantity:
            reasons.append("REDUCTION_EXCEEDS_OWNED_POSITION")

    def _opening_checks(self, request: RiskRequest, reasons: list[str]) -> None:
        policy = self._policy
        if policy.mode is OperatingMode.KILL_SWITCH:
            reasons.append("KILL_SWITCH_ACTIVE")
        elif policy.mode is OperatingMode.REDUCE_ONLY:
            reasons.append("REDUCE_ONLY_MODE")
        if request.market.upper() not in policy.allowed_markets:
            reasons.append("MARKET_NOT_ALLOWED")
        if request.intent.symbol not in policy.allowed_symbols:
            reasons.append("SYMBOL_NOT_ALLOWED")
        if request.intent.side.value not in policy.allowed_sides:
            reasons.append("SIDE_NOT_ALLOWED")
        if request.order_type.upper() not in policy.allowed_order_types:
            reasons.append("ORDER_TYPE_NOT_ALLOWED")
        if request.margin > policy.max_margin:
            reasons.append("MARGIN_LIMIT_EXCEEDED")
        if request.notional > policy.max_notional:
            reasons.append("NOTIONAL_LIMIT_EXCEEDED")
        if request.leverage > policy.max_leverage:
            reasons.append("LEVERAGE_LIMIT_EXCEEDED")
        if request.open_position_count >= policy.max_open_positions:
            reasons.append("POSITION_COUNT_LIMIT_REACHED")
        if request.available_balance - policy.balance_buffer < request.margin:
            reasons.append("BALANCE_BUFFER_BREACHED")
        if request.recent_order_count >= policy.max_orders_per_window:
            reasons.append("ORDER_RATE_LIMIT_REACHED")
        if request.consecutive_failures >= policy.max_consecutive_failures:
            reasons.append("FAILURE_THRESHOLD_REACHED")
        if request.daily_pnl <= -policy.daily_loss_limit:
            reasons.append("DAILY_LOSS_LIMIT_REACHED")

        age = Decimal(str((request.evaluated_at - request.market_time).total_seconds()))
        if age < 0 or age > policy.max_market_age_seconds:
            reasons.append("MARKET_DATA_STALE")
        deviation = (
            abs(request.expected_price - request.reference_price)
            / request.reference_price
            * Decimal("10000")
        )
        if deviation > policy.max_price_deviation_bps:
            reasons.append("PRICE_DEVIATION_EXCEEDED")
        self._stop_loss_checks(request, reasons)

    def _stop_loss_checks(self, request: RiskRequest, reasons: list[str]) -> None:
        policy = self._policy
        stop = request.stop_loss_price
        if stop is None:
            if policy.require_stop_loss:
                reasons.append("STOP_LOSS_REQUIRED")
            return
        if request.intent.position_side is PositionSide.LONG:
            stop_is_protective = stop < request.expected_price
        else:
            stop_is_protective = stop > request.expected_price
        if not stop_is_protective:
            reasons.append("STOP_LOSS_DIRECTION_INVALID")
        maximum_loss = abs(request.expected_price - stop) * request.intent.quantity
        if maximum_loss > policy.max_loss_per_trade:
            reasons.append("MAX_LOSS_EXCEEDED")
