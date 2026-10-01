from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import IO


class LeaseUnavailable(RuntimeError):
    """Another writer currently owns the strategy run."""


class RunLease:
    def __init__(
        self, path: str | Path, *, strategy_id: str, run_id: str, owner_id: str
    ):
        if not strategy_id or not run_id or not owner_id:
            raise ValueError("lease ownership identifiers are required")
        self.path = Path(path)
        self.strategy_id = strategy_id
        self.run_id = run_id
        self.owner_id = owner_id
        self._handle: IO[str] | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self.acquired:
            raise LeaseUnavailable("this lease object already holds the run")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise LeaseUnavailable("strategy run already has a writer") from exc
        self._handle = handle
        self.heartbeat()

    def heartbeat(self) -> None:
        if self._handle is None:
            raise LeaseUnavailable("lease is not acquired")
        metadata = {
            "schema_version": 1,
            "strategy_id": self.strategy_id,
            "run_id": self.run_id,
            "owner_id": self.owner_id,
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
        }
        self._handle.seek(0)
        self._handle.truncate()
        json.dump(metadata, self._handle, sort_keys=True)
        self._handle.write("\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def release(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    def __enter__(self) -> "RunLease":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
