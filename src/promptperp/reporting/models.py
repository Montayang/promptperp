from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from promptperp.accounting.models import ReportFrequency, require_utc


@dataclass(frozen=True)
class ReportPeriod:
    frequency: ReportFrequency
    start_at: datetime
    end_at: datetime
    timezone_name: str

    def __post_init__(self) -> None:
        require_utc(self.start_at, "period start")
        require_utc(self.end_at, "period end")
        if self.start_at >= self.end_at:
            raise ValueError("report period must be positive")


@dataclass(frozen=True)
class InvestorStatement:
    statement_id: str
    investor_id: str
    display_name: str
    pool_id: str
    strategy_id: str
    strategy_version: str
    period: ReportPeriod
    equity: Decimal
    net_contributions: Decimal
    cumulative_return_rate: Decimal
    unit_nav: Decimal
    snapshot_at: datetime
    reconciliation_id: str
    created_at: datetime

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "statement_id": self.statement_id,
            "investor_id": self.investor_id,
            "display_name": self.display_name,
            "pool_id": self.pool_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "frequency": self.period.frequency.value,
            "period_start": self.period.start_at.isoformat(),
            "period_end": self.period.end_at.isoformat(),
            "timezone": self.period.timezone_name,
            "equity": str(self.equity),
            "net_contributions": str(self.net_contributions),
            "cumulative_return_rate": str(self.cumulative_return_rate),
            "unit_nav": str(self.unit_nav),
            "snapshot_at": self.snapshot_at.isoformat(),
            "reconciliation_id": self.reconciliation_id,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class OutboxMessage:
    message_id: str
    statement_id: str
    recipient_email: str
    payload_text: str
    attempt_count: int
