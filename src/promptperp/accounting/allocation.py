from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal

from promptperp.accounting.errors import AccountingValidationError
from promptperp.accounting.models import UNIT_QUANTUM, require_money


def issue_units(amount: Decimal, unit_nav: Decimal) -> Decimal:
    require_money(amount, "contribution", positive=True)
    if not unit_nav.is_finite() or unit_nav <= 0:
        raise AccountingValidationError("unit_nav must be finite and positive")
    units = (amount / unit_nav).quantize(UNIT_QUANTUM, rounding=ROUND_DOWN)
    if units <= 0:
        raise AccountingValidationError("contribution produces no units")
    return units


def redeem_units(amount: Decimal, unit_nav: Decimal, owned_units: Decimal) -> Decimal:
    require_money(amount, "withdrawal", positive=True)
    if not unit_nav.is_finite() or unit_nav <= 0:
        raise AccountingValidationError("unit_nav must be finite and positive")
    if owned_units <= 0 or not owned_units.is_finite():
        raise AccountingValidationError("investor has no redeemable units")
    units = (amount / unit_nav).quantize(UNIT_QUANTUM, rounding=ROUND_UP)
    if units <= 0 or units > owned_units:
        raise AccountingValidationError("withdrawal exceeds investor equity")
    return units
