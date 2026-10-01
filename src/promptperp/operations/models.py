from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class LifecycleState(str, Enum):
    RUNNING = "RUNNING"
    STOP_REQUESTED = "STOP_REQUESTED"
    STOPPED = "STOPPED"


class HealthState(str, Enum):
    NORMAL = "NORMAL"
    ENTRY_DISABLED = "ENTRY_DISABLED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    BLOCKED = "BLOCKED"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class RunStatus:
    schema_version: int
    strategy_id: str
    run_id: str
    commit_sha: str
    lifecycle: LifecycleState
    health: HealthState
    started_at: datetime
    heartbeat_at: datetime
    updated_at: datetime
    owns_position: bool = False
    protection_confirmed: bool = False
    reconciliation_required: bool = False
    blocking_reasons: tuple[str, ...] = ()
    metrics: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported run status schema")
        if not self.strategy_id or not self.run_id or not self.commit_sha:
            raise ValueError("run status identity is required")
        timestamps = (self.started_at, self.heartbeat_at, self.updated_at)
        if any(value.tzinfo is None for value in timestamps):
            raise ValueError("run status timestamps must be timezone-aware")
        if self.heartbeat_at < self.started_at or self.updated_at < self.started_at:
            raise ValueError("run status timestamps are inconsistent")
        if self.protection_confirmed and not self.owns_position:
            raise ValueError("protection cannot exist without an owned position")
        if (
            self.health is HealthState.RECONCILIATION_REQUIRED
            and not self.reconciliation_required
        ) or (
            self.reconciliation_required
            and self.health
            not in {HealthState.RECONCILIATION_REQUIRED, HealthState.BLOCKED}
        ):
            raise ValueError("reconciliation flag and health disagree")
        if self.health is HealthState.BLOCKED and not self.blocking_reasons:
            raise ValueError("blocked status requires a reason")
        if (
            self.lifecycle is LifecycleState.STOPPED
            and self.health is not HealthState.STOPPED
        ):
            raise ValueError("stopped lifecycle requires stopped health")
        object.__setattr__(self, "blocking_reasons", tuple(self.blocking_reasons))
        sensitive = ("key", "secret", "password", "token", "authorization")
        if any(
            not isinstance(key, str)
            or not key
            or any(fragment in key.lower() for fragment in sensitive)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in self.metrics.items()
        ):
            raise ValueError(
                "status metrics must be non-sensitive non-negative counters"
            )
        object.__setattr__(self, "metrics", dict(self.metrics))

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("lifecycle", "health"):
            value[key] = getattr(self, key).value
        for key in ("started_at", "heartbeat_at", "updated_at"):
            value[key] = getattr(self, key).isoformat()
        return value


@dataclass(frozen=True)
class StatusInspection:
    status: RunStatus
    heartbeat_age_seconds: float
    stale: bool
    writer_active: bool
    detected_health: HealthState
    reasons: tuple[str, ...] = ()
