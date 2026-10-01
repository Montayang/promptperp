from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from promptperp.accounting.models import ReportFrequency, require_utc
from promptperp.reporting.models import OutboxMessage
from promptperp.reporting.schedules import next_delivery


class ReportingGenerator(Protocol):
    def generate(
        self,
        *,
        investor_id: str,
        frequency: ReportFrequency,
        timezone_name: str,
        due_at: datetime,
        monthly_send_day: int = 1,
    ) -> OutboxMessage: ...


class ScheduleStore(Protocol):
    def enabled_report_preferences(self) -> tuple[Any, ...]: ...

    def initialize_schedule(
        self, *, investor_id: str, next_due_at: datetime, occurred_at: datetime
    ) -> None: ...

    def advance_schedule(
        self,
        *,
        investor_id: str,
        expected_due_at: datetime,
        next_due_at: datetime,
        statement_id: str,
        occurred_at: datetime,
    ) -> bool: ...


@dataclass(frozen=True)
class ScheduleRunResult:
    initialized: int
    generated: int
    message_ids: tuple[str, ...]


class ReportScheduler:
    """Idempotent one-shot scheduler owned by the accounting writer."""

    def __init__(
        self,
        reporting: ReportingGenerator,
        store: ScheduleStore,
        *,
        maximum_catch_up: int = 31,
    ) -> None:
        if maximum_catch_up <= 0:
            raise ValueError("maximum_catch_up must be positive")
        self.reporting = reporting
        self.store = store
        self.maximum_catch_up = maximum_catch_up

    def run_due(self, *, now: datetime) -> ScheduleRunResult:
        require_utc(now, "scheduler now")
        initialized = generated = 0
        message_ids: list[str] = []
        for preference in self.store.enabled_report_preferences():
            investor_id = str(preference["investor_id"])
            frequency = ReportFrequency(str(preference["frequency"]))
            timezone_name = str(preference["timezone"])
            local_send_time = str(preference["local_send_time"])
            monthly_send_day = int(preference["monthly_send_day"])
            raw_due = preference["next_due_at"]
            if raw_due is None:
                due = next_delivery(
                    frequency,
                    after=now,
                    timezone_name=timezone_name,
                    local_send_time=local_send_time,
                    monthly_send_day=monthly_send_day,
                )
                self.store.initialize_schedule(
                    investor_id=investor_id,
                    next_due_at=due,
                    occurred_at=now,
                )
                initialized += 1
                continue

            due = datetime.fromisoformat(str(raw_due))
            attempts = 0
            while due <= now:
                if attempts >= self.maximum_catch_up:
                    raise RuntimeError(
                        f"report catch-up limit exceeded for {investor_id}"
                    )
                message = self.reporting.generate(
                    investor_id=investor_id,
                    frequency=frequency,
                    timezone_name=timezone_name,
                    due_at=due,
                    monthly_send_day=monthly_send_day,
                )
                following = next_delivery(
                    frequency,
                    after=due,
                    timezone_name=timezone_name,
                    local_send_time=local_send_time,
                    monthly_send_day=monthly_send_day,
                )
                advanced = self.store.advance_schedule(
                    investor_id=investor_id,
                    expected_due_at=due,
                    next_due_at=following,
                    statement_id=message.statement_id,
                    occurred_at=now,
                )
                if not advanced:
                    raise RuntimeError(
                        f"report schedule lease changed for {investor_id}"
                    )
                message_ids.append(message.message_id)
                generated += 1
                attempts += 1
                due = following
        return ScheduleRunResult(
            initialized=initialized,
            generated=generated,
            message_ids=tuple(message_ids),
        )
