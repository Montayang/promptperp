from __future__ import annotations

import fcntl
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

from promptperp.execution import RunLease
from promptperp.operations.models import (
    HealthState,
    LifecycleState,
    RunStatus,
    StatusInspection,
)
from promptperp.operations.store import StatusStore

Clock = Callable[[], datetime]


class SupervisorError(RuntimeError):
    pass


class RunSupervisor:
    def __init__(
        self,
        *,
        status_path: str | Path,
        lease: RunLease,
        commit_sha: str,
        stale_after: timedelta = timedelta(seconds=30),
        clock: Clock | None = None,
    ):
        if stale_after.total_seconds() <= 0:
            raise ValueError("stale threshold must be positive")
        if not commit_sha:
            raise ValueError("commit identity is required")
        self.store = StatusStore(status_path)
        self.lease = lease
        self.commit_sha = commit_sha
        self.stale_after = stale_after
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._status: RunStatus | None = None

    @property
    def status(self) -> RunStatus | None:
        return self._status

    def start(self) -> RunStatus:
        if self._status is not None:
            raise SupervisorError("supervisor instance already started")
        self.lease.acquire()
        now = self._now()
        status = RunStatus(
            schema_version=1,
            strategy_id=self.lease.strategy_id,
            run_id=self.lease.run_id,
            commit_sha=self.commit_sha,
            lifecycle=LifecycleState.RUNNING,
            health=HealthState.NORMAL,
            started_at=now,
            heartbeat_at=now,
            updated_at=now,
        )
        try:
            self.store.write(status)
        except Exception:
            self.lease.release()
            raise
        self._status = status
        return status

    def heartbeat(
        self,
        *,
        health: HealthState | None = None,
        owns_position: bool | None = None,
        protection_confirmed: bool | None = None,
        reconciliation_required: bool | None = None,
        blocking_reasons: tuple[str, ...] | None = None,
        metrics: Mapping[str, int] | None = None,
    ) -> RunStatus:
        current = self._require_running()
        if health is HealthState.STOPPED:
            raise SupervisorError("heartbeat cannot mark a run stopped")
        new_health = health or current.health
        new_reconciliation = (
            current.reconciliation_required
            if reconciliation_required is None
            else reconciliation_required
        )
        new_reasons = (
            current.blocking_reasons if blocking_reasons is None else blocking_reasons
        )
        if health is not None and reconciliation_required is None:
            new_reconciliation = health is HealthState.RECONCILIATION_REQUIRED
        if (
            health is not None
            and blocking_reasons is None
            and health is not HealthState.BLOCKED
        ):
            new_reasons = ()
        if new_reconciliation:
            new_health = HealthState.RECONCILIATION_REQUIRED
        if new_reasons:
            new_health = HealthState.BLOCKED
        now = self._now()
        status = replace(
            current,
            health=new_health,
            heartbeat_at=now,
            updated_at=now,
            owns_position=current.owns_position
            if owns_position is None
            else owns_position,
            protection_confirmed=(
                current.protection_confirmed
                if protection_confirmed is None
                else protection_confirmed
            ),
            reconciliation_required=new_reconciliation,
            blocking_reasons=new_reasons,
            metrics=dict(metrics or current.metrics),
        )
        self.lease.heartbeat()
        self.store.write(status)
        self._status = status
        return status

    def request_stop(self, *, reason: str) -> RunStatus:
        current = self._require_running()
        if not reason:
            raise ValueError("stop reason is required")
        now = self._now()
        unresolved = current.health in {
            HealthState.BLOCKED,
            HealthState.RECONCILIATION_REQUIRED,
        }
        if current.owns_position or unresolved:
            stop_health = current.health if unresolved else HealthState.ENTRY_DISABLED
            reasons = tuple(dict.fromkeys((*current.blocking_reasons, reason)))
            status = replace(
                current,
                lifecycle=LifecycleState.STOP_REQUESTED,
                health=stop_health,
                updated_at=now,
                blocking_reasons=reasons,
            )
        else:
            status = replace(
                current,
                lifecycle=LifecycleState.STOPPED,
                health=HealthState.STOPPED,
                updated_at=now,
                blocking_reasons=(reason,),
            )
        self.store.write(status)
        self._status = status
        self.lease.release()
        return status

    @staticmethod
    def inspect(
        *,
        status_path: str | Path,
        lease_path: str | Path,
        now: datetime | None = None,
        stale_after: timedelta = timedelta(seconds=30),
    ) -> StatusInspection:
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            raise ValueError("inspection time must be timezone-aware")
        status = StatusStore(status_path).read()
        writer_active = RunSupervisor._writer_active(lease_path)
        age = (current_time - status.heartbeat_at).total_seconds()
        stale = age > stale_after.total_seconds()
        reasons: list[str] = []
        detected = status.health
        if status.lifecycle is LifecycleState.RUNNING and stale:
            detected = HealthState.BLOCKED
            reasons.append("HEARTBEAT_STALE")
        if status.lifecycle is LifecycleState.RUNNING and not writer_active:
            detected = HealthState.BLOCKED
            reasons.append("WRITER_MISSING")
        return StatusInspection(
            status=status,
            heartbeat_age_seconds=max(age, 0.0),
            stale=stale,
            writer_active=writer_active,
            detected_health=detected,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _writer_active(lease_path: str | Path) -> bool:
        try:
            descriptor = os.open(lease_path, os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            os.close(descriptor)

    def _require_running(self) -> RunStatus:
        if self._status is None or not self.lease.acquired:
            raise SupervisorError("supervisor does not own an active run")
        if self._status.lifecycle is not LifecycleState.RUNNING:
            raise SupervisorError("run is no longer active")
        return self._status

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("supervisor clock must be timezone-aware")
        return value
