from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

_SECRET_KEYS = {
    "api_key",
    "api_secret",
    "authorization",
    "email_password",
    "password",
    "secret",
    "smtp_password",
    "token",
}
_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret|password|authorization|token)\s*[:=]\s*[^\s,;]+"
)


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]" if str(key).lower() in _SECRET_KEYS else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return _SECRET_PATTERN.sub(lambda match: match.group(1) + "=[REDACTED]", value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


def structured_log(event: str, fields: Mapping[str, Any]) -> str:
    if not event:
        raise ValueError("log event is required")
    record = {"event": event, **dict(fields)}
    return json.dumps(redact(record), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class RunReport:
    schema_version: int
    strategy_id: str
    run_id: str
    commit_sha: str
    started_at: datetime
    ended_at: datetime
    final_health: str
    intents: int
    settled_trades: int
    realized_pnl: Decimal
    commission: Decimal
    funding: Decimal
    stop_reason: str
    unresolved_ownership: bool

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not all(
            (
                self.strategy_id,
                self.run_id,
                self.commit_sha,
                self.final_health,
                self.stop_reason,
            )
        ):
            raise ValueError("run report identity and outcome are required")
        if self.started_at.tzinfo is None or self.ended_at.tzinfo is None:
            raise ValueError("run report timestamps must be timezone-aware")
        if (
            self.ended_at < self.started_at
            or min(self.intents, self.settled_trades) < 0
        ):
            raise ValueError("run report chronology or counters are invalid")
        if self.settled_trades > self.intents:
            raise ValueError("settled trades cannot exceed intents")
        if not all(
            x.is_finite() for x in (self.realized_pnl, self.commission, self.funding)
        ):
            raise ValueError("run report financial values must be finite")

    def to_json(self) -> str:
        return json.dumps(redact(asdict(self)), sort_keys=True, separators=(",", ":"))
