from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from promptperp.accounting.errors import AccountingValidationError
from promptperp.accounting.models import MONEY_QUANTUM, UNIT_QUANTUM, ValuationSnapshot


def value_pool(
    *,
    snapshot_id: str,
    pool_id: str,
    occurred_at: datetime,
    cash: Decimal,
    unrealized_pnl: Decimal,
    outstanding_units: Decimal,
    reconciliation_id: str,
) -> ValuationSnapshot:
    if outstanding_units <= 0:
        raise AccountingValidationError("cannot value a pool without units")
    equity = (cash + unrealized_pnl).quantize(MONEY_QUANTUM)
    if equity <= 0:
        raise AccountingValidationError("pool equity must remain positive")
    nav = (equity / outstanding_units).quantize(UNIT_QUANTUM)
    return ValuationSnapshot(
        snapshot_id=snapshot_id,
        pool_id=pool_id,
        occurred_at=occurred_at,
        pool_equity=equity,
        outstanding_units=outstanding_units,
        unit_nav=nav,
        reconciliation_id=reconciliation_id,
    )
