from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from promptperp.domain import ReconciliationFailed


class SharedAccountBusy(ReconciliationFailed):
    """No lock acquired and no account action attempted; safe to defer a poll."""


class SharedAccountTurn:
    """Serialize account reconciliation and mutations across strategy workers."""

    def __init__(self, directory: str | Path, *, timeout_seconds: float = 30):
        if not 0 < timeout_seconds <= 60:
            raise ValueError("shared lock timeout must be bounded")
        self.directory = Path(directory)
        self.barrier = self.directory / "unresolved.json"
        self.timeout_seconds = timeout_seconds

    def verify_barrier(self, run_id: str) -> None:
        if self.barrier.exists():
            try:
                value = json.loads(self.barrier.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ReconciliationFailed(
                    "shared execution barrier is unreadable"
                ) from exc
            if value.get("run_id") != run_id:
                raise ReconciliationFailed(
                    "shared execution barrier belongs to another run"
                )

    @contextmanager
    def acquire(self, *, recovery: bool = False) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            self.directory / "account.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise SharedAccountBusy(
                            "shared account turn timed out"
                        ) from exc
                    time.sleep(0.05)
            if self.barrier.exists() and not recovery:
                raise ReconciliationFailed("shared account has an unresolved execution")
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
