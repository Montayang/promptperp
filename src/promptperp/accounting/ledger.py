from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from promptperp.accounting import chart
from promptperp.accounting.errors import AccountingValidationError
from promptperp.accounting.models import (
    EntryDirection,
    LedgerTransaction,
    Posting,
    TradingSettlement,
)


def contribution_transaction(
    *,
    transaction_id: str,
    investor_id: str,
    pool_id: str,
    amount: Decimal,
    occurred_at: datetime,
    actor: str,
    reason: str,
    external_reference: str,
) -> LedgerTransaction:
    return LedgerTransaction(
        transaction_id=transaction_id,
        kind="CONTRIBUTION",
        occurred_at=occurred_at,
        actor=actor,
        reason=reason,
        external_reference=external_reference,
        postings=(
            Posting(chart.pool_cash(pool_id), EntryDirection.DEBIT, amount),
            Posting(chart.investor_capital(investor_id), EntryDirection.CREDIT, amount),
        ),
    )


def withdrawal_transaction(
    *,
    transaction_id: str,
    investor_id: str,
    pool_id: str,
    amount: Decimal,
    occurred_at: datetime,
    actor: str,
    reason: str,
    external_reference: str,
) -> LedgerTransaction:
    return LedgerTransaction(
        transaction_id=transaction_id,
        kind="WITHDRAWAL",
        occurred_at=occurred_at,
        actor=actor,
        reason=reason,
        external_reference=external_reference,
        postings=(
            Posting(
                chart.investor_withdrawals(investor_id), EntryDirection.DEBIT, amount
            ),
            Posting(chart.pool_cash(pool_id), EntryDirection.CREDIT, amount),
        ),
    )


def _signed_income_postings(
    pool_id: str, account: str, amount: Decimal
) -> tuple[Posting, ...]:
    if amount > 0:
        return (
            Posting(chart.pool_cash(pool_id), EntryDirection.DEBIT, amount),
            Posting(account, EntryDirection.CREDIT, amount),
        )
    if amount < 0:
        loss = -amount
        return (
            Posting(account, EntryDirection.DEBIT, loss),
            Posting(chart.pool_cash(pool_id), EntryDirection.CREDIT, loss),
        )
    return ()


def settlement_transaction(
    *, pool_id: str, settlement: TradingSettlement
) -> LedgerTransaction:
    postings: list[Posting] = []
    postings.extend(
        _signed_income_postings(
            pool_id, chart.realized_pnl(pool_id), settlement.realized_pnl
        )
    )
    if settlement.commission > 0:
        postings.extend(
            (
                Posting(
                    chart.trading_fees(pool_id),
                    EntryDirection.DEBIT,
                    settlement.commission,
                ),
                Posting(
                    chart.pool_cash(pool_id),
                    EntryDirection.CREDIT,
                    settlement.commission,
                ),
            )
        )
    postings.extend(
        _signed_income_postings(pool_id, chart.funding(pool_id), settlement.funding)
    )
    if not postings:
        raise AccountingValidationError("settlement has no economic effect")
    return LedgerTransaction(
        transaction_id=f"settlement:{settlement.event_id}",
        kind="TRADING_SETTLEMENT",
        occurred_at=settlement.occurred_at,
        actor="execution-ledger",
        reason="confirmed owned trading settlement",
        external_reference=settlement.event_id,
        postings=tuple(postings),
    )
