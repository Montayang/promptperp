from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from promptperp.accounting.errors import CashFlowBlocked
from promptperp.accounting.models import (
    CashFlowGate,
    ReconciliationResult,
    ReconciliationStatus,
    require_money,
    require_utc,
)

DEFAULT_MAX_RECONCILIATION_AGE = timedelta(minutes=15)
DEFAULT_TOLERANCE = Decimal("0.00000001")


def require_cash_flow_window(
    gate: CashFlowGate,
    *,
    maximum_age: timedelta = DEFAULT_MAX_RECONCILIATION_AGE,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> None:
    reasons: list[str] = []
    if gate.has_managed_position:
        reasons.append("managed position exists")
    if gate.has_foreign_position:
        reasons.append("foreign position exists")
    if gate.has_open_order:
        reasons.append("open order exists")
    if gate.has_unsettled_event:
        reasons.append("unsettled event exists")
    if gate.has_unknown_result:
        reasons.append("unknown execution result exists")
    if abs(gate.unexplained_difference) > tolerance:
        reasons.append("unexplained account difference exists")
    if gate.reconciled_at is None:
        reasons.append("no successful reconciliation")
    elif gate.checked_at - gate.reconciled_at > maximum_age:
        reasons.append("successful reconciliation is stale")
    elif gate.reconciled_at > gate.checked_at:
        reasons.append("reconciliation timestamp is in the future")
    if gate.reconciliation_id is None:
        reasons.append("reconciliation identity is missing")
    if reasons:
        raise CashFlowBlocked("; ".join(reasons))


def reconcile(
    *,
    reconciliation_id: str,
    occurred_at: datetime,
    exchange_equity: Decimal,
    internal_equity: Decimal,
    blocking_reasons: tuple[str, ...] = (),
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> ReconciliationResult:
    require_utc(occurred_at, "occurred_at")
    require_money(exchange_equity, "exchange_equity")
    require_money(internal_equity, "internal_equity")
    if exchange_equity < 0:
        blocking_reasons += ("exchange equity is negative",)
    difference = exchange_equity - internal_equity
    reasons = list(blocking_reasons)
    if abs(difference) > tolerance:
        reasons.append("exchange and internal equity differ")
    status = (
        ReconciliationStatus.PASSED if not reasons else ReconciliationStatus.BLOCKED
    )
    return ReconciliationResult(
        reconciliation_id=reconciliation_id,
        occurred_at=occurred_at,
        exchange_equity=exchange_equity,
        internal_equity=internal_equity,
        difference=difference,
        status=status,
        reasons=tuple(reasons),
    )
