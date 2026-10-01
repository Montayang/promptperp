from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from promptperp.operations.models import HealthState, LifecycleState, RunStatus


class StatusCorrupted(RuntimeError):
    pass


class StatusStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def write(self, status: RunStatus) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            payload = (
                json.dumps(
                    status.public_dict(), sort_keys=True, separators=(",", ":")
                ).encode()
                + b"\n"
            )
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def read(self) -> RunStatus:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return self._decode(raw)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise StatusCorrupted("run status cannot be trusted") from exc

    @staticmethod
    def _decode(raw: Mapping[str, Any]) -> RunStatus:
        return RunStatus(
            schema_version=int(raw["schema_version"]),
            strategy_id=str(raw["strategy_id"]),
            run_id=str(raw["run_id"]),
            commit_sha=str(raw["commit_sha"]),
            lifecycle=LifecycleState(str(raw["lifecycle"])),
            health=HealthState(str(raw["health"])),
            started_at=datetime.fromisoformat(str(raw["started_at"])),
            heartbeat_at=datetime.fromisoformat(str(raw["heartbeat_at"])),
            updated_at=datetime.fromisoformat(str(raw["updated_at"])),
            owns_position=bool(raw.get("owns_position", False)),
            protection_confirmed=bool(raw.get("protection_confirmed", False)),
            reconciliation_required=bool(raw.get("reconciliation_required", False)),
            blocking_reasons=tuple(str(x) for x in raw.get("blocking_reasons", ())),
            metrics=dict(raw.get("metrics", {})),
        )
