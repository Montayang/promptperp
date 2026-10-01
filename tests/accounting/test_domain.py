from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from promptperp.accounting import (
    AccountingValidationError,
    CashFlowBlocked,
    CashFlowGate,
    EntryDirection,
    LedgerTransaction,
    Posting,
    issue_units,
    require_cash_flow_window,
)

NOW = datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc)


def test_double_entry_transaction_must_balance():
    with pytest.raises(AccountingValidationError, match="not balanced"):
        LedgerTransaction(
            transaction_id="tx-1",
            kind="TEST",
            occurred_at=NOW,
            actor="operator",
            reason="test invalid transaction",
            external_reference="evidence-1",
            postings=(
                Posting("asset:cash", EntryDirection.DEBIT, Decimal("10")),
                Posting("equity:capital", EntryDirection.CREDIT, Decimal("9")),
            ),
        )


def test_amount_precision_and_unit_precision_are_explicit():
    with pytest.raises(AccountingValidationError, match="money precision"):
        Posting("asset:cash", EntryDirection.DEBIT, Decimal("1.000000001"))

    assert issue_units(Decimal("1500"), Decimal("1")) == Decimal(
        "1500.000000000000000000"
    )


@pytest.mark.parametrize(
    "change, message",
    [
        ({"has_managed_position": True}, "managed position"),
        ({"has_foreign_position": True}, "foreign position"),
        ({"has_open_order": True}, "open order"),
        ({"has_unsettled_event": True}, "unsettled event"),
        ({"has_unknown_result": True}, "unknown execution"),
        ({"unexplained_difference": Decimal("0.01")}, "difference"),
    ],
)
def test_cash_flow_gate_fails_closed(change, message):
    values = {
        "checked_at": NOW,
        "reconciled_at": NOW,
        "reconciliation_id": "recon-test",
        "has_managed_position": False,
        "has_foreign_position": False,
        "has_open_order": False,
        "has_unsettled_event": False,
        "has_unknown_result": False,
        "unexplained_difference": Decimal("0"),
    }
    values.update(change)

    with pytest.raises(CashFlowBlocked, match=message):
        require_cash_flow_window(CashFlowGate(**values))


def test_cash_flow_gate_rejects_stale_reconciliation():
    gate = CashFlowGate(
        checked_at=NOW,
        reconciled_at=NOW - timedelta(minutes=16),
        reconciliation_id="recon-stale",
    )
    with pytest.raises(CashFlowBlocked, match="stale"):
        require_cash_flow_window(gate)
