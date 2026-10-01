from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from promptperp.accounting.errors import AccountingValidationError
from promptperp.accounting.models import ReportFrequency
from promptperp.accounting.service import InvestorAccountingService
from promptperp.reporting.models import InvestorStatement, OutboxMessage
from promptperp.reporting.schedules import completed_period
from promptperp.reporting.statements import build_statement, render_statement_text


class StatementStore(Protocol):
    def enqueue_statement(
        self, statement: InvestorStatement, payload_text: str
    ) -> OutboxMessage: ...


class InvestorReportingService:
    """Runs inside the sole accounting writer process."""

    def __init__(
        self,
        accounting: InvestorAccountingService,
        store: StatementStore,
        *,
        maximum_snapshot_age: timedelta = timedelta(minutes=15),
    ):
        if maximum_snapshot_age <= timedelta(0):
            raise ValueError("maximum_snapshot_age must be positive")
        self.accounting = accounting
        self.store = store
        self.maximum_snapshot_age = maximum_snapshot_age

    def generate(
        self,
        *,
        investor_id: str,
        frequency: ReportFrequency,
        timezone_name: str,
        due_at: datetime,
        monthly_send_day: int = 1,
    ) -> OutboxMessage:
        view = self.accounting.investor_view(investor_id)
        if (
            view.snapshot_at > due_at
            or due_at - view.snapshot_at > self.maximum_snapshot_age
        ):
            raise AccountingValidationError(
                "statement requires a recent reconciliation at or before due_at"
            )
        period = completed_period(
            frequency,
            due_at=due_at,
            timezone_name=timezone_name,
            monthly_send_day=monthly_send_day,
        )
        statement = build_statement(view, period=period, created_at=due_at)
        return self.store.enqueue_statement(statement, render_statement_text(statement))
