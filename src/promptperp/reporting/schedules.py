from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from promptperp.accounting.models import ReportFrequency, require_utc
from promptperp.reporting.models import ReportPeriod


def _parse_time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("local send time must use HH:MM[:SS]") from exc
    if parsed.tzinfo is not None:
        raise ValueError("local send time cannot include a timezone")
    return parsed


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _previous_month_start(value: date) -> date:
    current = _month_start(value)
    return (current - timedelta(days=1)).replace(day=1)


def completed_period(
    frequency: ReportFrequency,
    *,
    due_at: datetime,
    timezone_name: str,
    monthly_send_day: int = 1,
) -> ReportPeriod:
    require_utc(due_at, "due_at")
    zone = ZoneInfo(timezone_name)
    local_date = due_at.astimezone(zone).date()
    if frequency is ReportFrequency.DAILY:
        end_date = local_date
        start_date = end_date - timedelta(days=1)
    elif frequency is ReportFrequency.WEEKLY:
        end_date = local_date - timedelta(days=local_date.weekday())
        start_date = end_date - timedelta(days=7)
    else:
        if not 1 <= monthly_send_day <= 28:
            raise ValueError("monthly send day must be between 1 and 28")
        this_month_day = local_date.replace(day=monthly_send_day)
        if local_date < this_month_day:
            previous_month = _previous_month_start(local_date)
            end_date = previous_month.replace(day=monthly_send_day)
        else:
            end_date = this_month_day
        previous_end_month = _previous_month_start(end_date)
        start_date = previous_end_month.replace(day=monthly_send_day)
    start = datetime.combine(start_date, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(end_date, time.min, zone).astimezone(timezone.utc)
    return ReportPeriod(
        frequency=frequency,
        start_at=start,
        end_at=end,
        timezone_name=timezone_name,
    )


def next_delivery(
    frequency: ReportFrequency,
    *,
    after: datetime,
    timezone_name: str,
    local_send_time: str,
    monthly_send_day: int = 1,
) -> datetime:
    require_utc(after, "after")
    zone = ZoneInfo(timezone_name)
    clock = _parse_time(local_send_time)
    local_after = after.astimezone(zone)
    candidate_date = local_after.date()
    if frequency is ReportFrequency.WEEKLY:
        candidate_date += timedelta(days=(-candidate_date.weekday()) % 7)
    elif frequency is ReportFrequency.MONTHLY:
        if not 1 <= monthly_send_day <= 28:
            raise ValueError("monthly send day must be between 1 and 28")
        candidate_date = candidate_date.replace(day=monthly_send_day)
    candidate = datetime.combine(candidate_date, clock, zone)
    if candidate <= local_after:
        if frequency is ReportFrequency.DAILY:
            candidate += timedelta(days=1)
        elif frequency is ReportFrequency.WEEKLY:
            candidate += timedelta(days=7)
        else:
            next_month = candidate.replace(day=28) + timedelta(days=4)
            candidate = datetime.combine(
                next_month.date().replace(day=monthly_send_day), clock, zone
            )
    return candidate.astimezone(timezone.utc)
