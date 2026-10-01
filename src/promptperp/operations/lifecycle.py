from __future__ import annotations

from enum import Enum
from typing import Any

from promptperp.operations.supervisor import RunSupervisor


class LifecycleCommand(str, Enum):
    START = "start"
    HEARTBEAT = "heartbeat"
    STATUS = "status"
    STOP = "stop"


class LifecycleController:
    """Narrow command surface; it has no exchange or notification dependency."""

    def __init__(self, supervisor: RunSupervisor):
        self.supervisor = supervisor

    def execute(self, command: LifecycleCommand, *, reason: str = "") -> dict[str, Any]:
        if command is LifecycleCommand.START:
            return self.supervisor.start().public_dict()
        if command is LifecycleCommand.HEARTBEAT:
            return self.supervisor.heartbeat().public_dict()
        if command is LifecycleCommand.STATUS:
            inspection = RunSupervisor.inspect(
                status_path=self.supervisor.store.path,
                lease_path=self.supervisor.lease.path,
                stale_after=self.supervisor.stale_after,
                now=self.supervisor.clock(),
            )
            return {
                "status": inspection.status.public_dict(),
                "heartbeat_age_seconds": inspection.heartbeat_age_seconds,
                "stale": inspection.stale,
                "writer_active": inspection.writer_active,
                "detected_health": inspection.detected_health.value,
                "reasons": list(inspection.reasons),
            }
        if command is LifecycleCommand.STOP:
            return self.supervisor.request_stop(reason=reason).public_dict()
        raise ValueError("unsupported lifecycle command")
