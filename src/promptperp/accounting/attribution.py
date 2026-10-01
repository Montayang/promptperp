from __future__ import annotations

from promptperp.accounting.errors import AccountingValidationError
from promptperp.accounting.models import TradingSettlement


def require_owned_settlement(
    settlement: TradingSettlement,
    *,
    expected_strategy_id: str,
) -> None:
    if settlement.strategy_id != expected_strategy_id:
        raise AccountingValidationError("settlement strategy ownership mismatch")
