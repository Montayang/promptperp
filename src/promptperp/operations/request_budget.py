"""Durable, conservative request-weight admission shared by local workers."""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


class BudgetUnavailable(RuntimeError):
    """No capacity granted; do not issue a request or acquire the account lock."""


@dataclass
class RequestPermit:
    """Process-local allowance; never persist or reuse after a process restart.

    Consume immediately before each request (including retries). Unused capacity
    is deliberately not refunded. One permit must not span the window boundary.
    """

    remaining: int
    expires_at: float
    issued_at: float
    clock: Callable[[], float] = field(repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_seen: float | None = field(default=None, init=False, repr=False)

    def consume(self, weight: int) -> None:
        with self._lock:
            now = self.clock()
            if (
                type(weight) is not int
                or weight <= 0
                or not math.isfinite(now)
                or not self.issued_at <= now < self.expires_at
                or (self._last_seen is not None and now < self._last_seen)
                or weight > self.remaining
            ):
                raise BudgetUnavailable("request permit expired or exhausted")
            self.remaining -= weight
            self._last_seen = now


class RequestBudget:
    """Reserve the whole worst-case operation before entering an account turn.

    Include preflight, every bounded confirmation read, final full settlement,
    and the second account snapshot. All workers for the same exchange/IP limit
    must use the same database. This is not a distributed rate limiter or a
    substitute for exchange limit headers; callers supply reviewed weights.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        capacity: int,
        window_seconds: float,
        clock: Callable[[], float] = time.time,
    ):
        if (
            type(capacity) is not int
            or capacity <= 0
            or not math.isfinite(window_seconds)
            or window_seconds <= 0
        ):
            raise ValueError("invalid request budget policy")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.capacity, self.window, self.clock = capacity, window_seconds, clock
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS policy "
                "(id INTEGER PRIMARY KEY CHECK(id=1), capacity INTEGER, "
                "window REAL, last_time REAL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS reservations "
                "(expires REAL NOT NULL, weight INTEGER NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO policy VALUES (1, ?, ?, 0)",
                (capacity, window_seconds),
            )
            self._policy(db)

    def _policy(self, db: sqlite3.Connection) -> float:
        row = db.execute("SELECT capacity, window, last_time FROM policy").fetchall()
        if len(row) != 1 or row[0][:2] != (self.capacity, self.window):
            raise BudgetUnavailable("shared request budget policy mismatch")
        last = float(row[0][2])
        if not math.isfinite(last):
            raise BudgetUnavailable("invalid request budget clock")
        return last

    def reserve(self, weight: int) -> RequestPermit:
        if type(weight) is not int or not 0 < weight <= self.capacity:
            raise BudgetUnavailable("operation cannot fit request budget")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            last = self._policy(db)
            now = self.clock()
            if not math.isfinite(now) or now < last:
                raise BudgetUnavailable("request budget clock moved backwards")
            db.execute("DELETE FROM reservations WHERE expires <= ?", (now,))
            used = db.execute(
                "SELECT COALESCE(SUM(weight), 0) FROM reservations"
            ).fetchone()[0]
            if used + weight > self.capacity:
                raise BudgetUnavailable("request budget exhausted; defer outside lock")
            # A permit may consume late in its window. Keep its reservation for
            # one extra window, so a late request still counts in the next one.
            db.execute(
                "INSERT INTO reservations VALUES (?, ?)",
                (now + 2 * self.window, weight),
            )
            db.execute("UPDATE policy SET last_time = ?", (now,))
        return RequestPermit(weight, now + self.window, now, self.clock)
