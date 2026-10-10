"""Persist a bounded no-trading hold for narrowly classified settlement lag."""

from __future__ import annotations

import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Callable

from promptperp.domain import ReconciliationFailed


class ValuationPending(ReconciliationFailed):
    """Ownership and orders are verified, but settlement valuation is not yet exact."""


class SettlementGate:
    """A restart cannot reset the grace period or clear an existing fault.

    Call under the shared account lock, after request-budget admission. The
    callback must do a FULL read-only reconciliation, with bounded I/O timeouts.
    Only ValuationPending is deferrable; unknown orders/positions are not.
    Success is not trading authorization and does not clear an operator stop.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_wait_seconds: float,
        clock: Callable[[], float] = time.time,
    ):
        if not math.isfinite(max_wait_seconds) or max_wait_seconds <= 0:
            raise ValueError("settlement wait must be finite and positive")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_wait, self.clock = max_wait_seconds, clock
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS settlement "
                "(id INTEGER PRIMARY KEY CHECK(id=1), max_wait REAL, "
                "pending_since REAL, last_time REAL, fault INTEGER)"
            )
            db.execute(
                "INSERT OR IGNORE INTO settlement VALUES (1, ?, NULL, 0, 0)",
                (max_wait_seconds,),
            )

    def reconcile(self, verify: Callable[[], None]) -> None:
        failure: Exception | None = None
        # The transaction commits the fault/hold BEFORE raising to the caller.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT max_wait, pending_since, last_time, fault FROM settlement"
            ).fetchall()
            if len(rows) != 1:
                raise ReconciliationFailed("settlement gate state is invalid")
            policy, pending, last, fault = rows[0]
            now = self.clock()
            if (
                policy != self.max_wait
                or fault != 0
                or not math.isfinite(now)
                or not math.isfinite(last)
                or now < last
                or (
                    pending is not None
                    and (not math.isfinite(pending) or not 0 <= pending <= now)
                )
            ):
                failure = ReconciliationFailed("settlement gate is faulted or invalid")
            elif pending is not None and now - pending >= self.max_wait:
                failure = ReconciliationFailed("settlement hold expired")
            else:
                try:
                    verify()
                except ValuationPending as exc:
                    pending = now if pending is None else pending
                    failure = exc
                except Exception as exc:
                    failure = exc
                finished = self.clock()
                if not math.isfinite(finished) or finished < now:
                    failure = ReconciliationFailed("settlement clock moved backwards")
                elif pending is not None and finished - pending >= self.max_wait:
                    failure = ReconciliationFailed("settlement hold expired")
                else:
                    now = finished
                if failure is None:
                    pending = None
            hard_fault = failure is not None and not isinstance(
                failure, ValuationPending
            )
            db.execute(
                "UPDATE settlement SET pending_since=?, last_time=?, fault=?",
                (pending, now if math.isfinite(now) else last, int(hard_fault)),
            )
        if failure is not None:
            raise failure
