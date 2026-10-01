from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping

from promptperp.domain import PositionSide
from promptperp.runtime.models import (
    ClaimState,
    ExecutionOutcome,
    PlatformMode,
    PlatformRunState,
    PlatformRunStatus,
    PositionClaim,
    RunPlan,
)


class PlatformStateError(RuntimeError):
    pass


class SQLitePlatformStore:
    """Single-host transactional control plane; contains no exchange capability."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 5000")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            connection.close()
            raise PlatformStateError("platform state requires SQLite foreign keys")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("PRAGMA query_only = ON")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def write_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.write_connection() as connection:
            mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
            if mode.lower() != "wal":
                raise PlatformStateError("platform state requires SQLite WAL mode")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS platform_schema (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_control (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    mode TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_runs (
                    run_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    plugin_fingerprint TEXT NOT NULL,
                    parameter_fingerprint TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    capital_budget TEXT NOT NULL,
                    max_margin_per_trade TEXT NOT NULL,
                    max_leverage INTEGER NOT NULL,
                    max_loss_per_trade TEXT NOT NULL,
                    max_positions INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    updated_at TEXT NOT NULL,
                    blocking_reasons_json TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_strategy
                ON platform_runs(strategy_id)
                WHERE state IN (
                    'PLANNED','RUNNING','STOP_REQUESTED','BLOCKED',
                    'BLOCKED_STOP_REQUESTED'
                );
                CREATE TABLE IF NOT EXISTS position_claims (
                    symbol TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    run_id TEXT NOT NULL REFERENCES platform_runs(run_id),
                    position_side TEXT NOT NULL,
                    margin TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    state TEXT NOT NULL,
                    protected INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_outcomes (
                    outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES platform_runs(run_id),
                    symbol TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    expected_price TEXT NOT NULL,
                    average_price TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    commission TEXT NOT NULL,
                    funding TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    anomalies_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    run_id TEXT,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO platform_schema(version, applied_at) VALUES (1, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            versions = connection.execute(
                "SELECT version FROM platform_schema ORDER BY version"
            ).fetchall()
            if [int(row["version"]) for row in versions] != [1]:
                raise PlatformStateError("unsupported platform state schema")
            if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                raise PlatformStateError("platform state integrity check failed")
            connection.execute(
                """
                INSERT OR IGNORE INTO platform_control(
                    singleton, mode, revision, reason, updated_at
                ) VALUES (1, 'NORMAL', 1, 'initialization', ?)
                """,
                (datetime.now(timezone.utc).isoformat(),),
            )
        os.chmod(self.path, 0o600)
        for suffix in ("-wal", "-shm"):
            companion = Path(str(self.path) + suffix)
            if companion.exists():
                os.chmod(companion, 0o600)

    def mode(self) -> PlatformMode:
        with self.read_connection() as connection:
            row = connection.execute(
                "SELECT mode FROM platform_control WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise PlatformStateError("platform control is not initialized")
        return PlatformMode(str(row["mode"]))

    def set_mode(
        self, mode: PlatformMode, *, reason: str, occurred_at: datetime
    ) -> None:
        if not reason or occurred_at.tzinfo is None:
            raise ValueError("mode change reason and aware timestamp are required")
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE platform_control
                SET mode = ?, revision = revision + 1, reason = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (mode.value, reason, occurred_at.isoformat()),
            )
            self._event(
                connection,
                "PLATFORM_MODE_CHANGED",
                None,
                {"mode": mode.value, "reason": reason},
                occurred_at,
            )

    def create_plan(self, plan: RunPlan) -> None:
        payload = json.dumps(
            dict(plan.parameters), sort_keys=True, separators=(",", ":"), default=str
        )
        with self.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO platform_runs(
                        run_id, strategy_id, plugin_fingerprint,
                        parameter_fingerprint, parameters_json, capital_budget,
                        max_margin_per_trade, max_leverage, max_loss_per_trade,
                        max_positions, state, created_at, updated_at,
                        blocking_reasons_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PLANNED', ?, ?, '[]')
                    """,
                    (
                        plan.run_id,
                        plan.strategy_id,
                        plan.plugin_fingerprint,
                        plan.parameter_fingerprint,
                        payload,
                        str(plan.capital_budget),
                        str(plan.max_margin_per_trade),
                        plan.max_leverage,
                        str(plan.max_loss_per_trade),
                        plan.max_positions,
                        plan.created_at.isoformat(),
                        plan.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PlatformStateError(
                    "run identity or active strategy is already planned"
                ) from exc
            self._event(connection, "RUN_PLANNED", plan.run_id, {}, plan.created_at)

    def start(self, run_id: str, *, occurred_at: datetime) -> PlatformRunStatus:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE platform_runs SET state = 'RUNNING', started_at = ?, updated_at = ?
                WHERE run_id = ? AND state = 'PLANNED'
                """,
                (occurred_at.isoformat(), occurred_at.isoformat(), run_id),
            )
            if cursor.rowcount != 1:
                raise PlatformStateError("only a planned run can start")
            self._event(connection, "RUN_STARTED", run_id, {}, occurred_at)
        return self.status(run_id)

    def status(self, run_id: str) -> PlatformRunStatus:
        with self.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM platform_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise PlatformStateError("run does not exist")
            reserved = connection.execute(
                "SELECT margin FROM position_claims WHERE run_id = ?", (run_id,)
            ).fetchall()
        return PlatformRunStatus(
            strategy_id=str(row["strategy_id"]),
            run_id=str(row["run_id"]),
            state=PlatformRunState(str(row["state"])),
            capital_budget=Decimal(str(row["capital_budget"])),
            reserved_margin=sum(
                (Decimal(str(item["margin"])) for item in reserved), Decimal("0")
            ),
            started_at=None
            if row["started_at"] is None
            else datetime.fromisoformat(str(row["started_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            blocking_reasons=tuple(json.loads(str(row["blocking_reasons_json"]))),
        )

    def plan(self, run_id: str) -> RunPlan:
        with self.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM platform_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise PlatformStateError("run does not exist")
        return RunPlan(
            strategy_id=str(row["strategy_id"]),
            run_id=str(row["run_id"]),
            plugin_fingerprint=str(row["plugin_fingerprint"]),
            parameter_fingerprint=str(row["parameter_fingerprint"]),
            parameters=json.loads(str(row["parameters_json"])),
            capital_budget=Decimal(str(row["capital_budget"])),
            max_margin_per_trade=Decimal(str(row["max_margin_per_trade"])),
            max_leverage=int(row["max_leverage"]),
            max_loss_per_trade=Decimal(str(row["max_loss_per_trade"])),
            max_positions=int(row["max_positions"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def update_run_allocation(
        self,
        *,
        run_id: str,
        capital_budget: Decimal,
        max_margin_per_trade: Decimal,
        max_leverage: int,
        max_loss_per_trade: Decimal,
        max_positions: int,
        occurred_at: datetime,
    ) -> None:
        if (
            capital_budget <= 0
            or max_margin_per_trade <= 0
            or max_margin_per_trade > capital_budget
            or max_leverage <= 0
            or max_loss_per_trade <= 0
            or max_positions <= 0
        ):
            raise ValueError("updated run allocation is invalid")
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT state, blocking_reasons_json FROM platform_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None or str(run["state"]) not in {
                PlatformRunState.PLANNED.value,
                PlatformRunState.RUNNING.value,
            }:
                raise PlatformStateError("only planned or running runs can rebalance")
            claim_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM position_claims WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            )
            if claim_count:
                raise PlatformStateError("run allocation can change only while flat")
            connection.execute(
                """
                UPDATE platform_runs
                SET capital_budget = ?, max_margin_per_trade = ?, max_leverage = ?,
                    max_loss_per_trade = ?, max_positions = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    str(capital_budget),
                    str(max_margin_per_trade),
                    max_leverage,
                    str(max_loss_per_trade),
                    max_positions,
                    occurred_at.isoformat(),
                    run_id,
                ),
            )
            self._event(
                connection,
                "RUN_ALLOCATION_UPDATED",
                run_id,
                {
                    "capital_budget": str(capital_budget),
                    "max_margin_per_trade": str(max_margin_per_trade),
                    "max_leverage": max_leverage,
                    "max_loss_per_trade": str(max_loss_per_trade),
                    "max_positions": max_positions,
                },
                occurred_at,
            )

    def claims(self) -> tuple[PositionClaim, ...]:
        with self.read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM position_claims ORDER BY symbol"
            ).fetchall()
        return tuple(self._claim(row) for row in rows)

    def add_claim(
        self,
        claim: PositionClaim,
        *,
        maximum_total_positions: int,
        maximum_total_margin: Decimal,
        occurred_at: datetime,
    ) -> None:
        if maximum_total_positions <= 0 or maximum_total_margin <= 0:
            raise ValueError("aggregate claim limits must be positive")
        if claim.state is not ClaimState.RESERVED:
            raise PlatformStateError("new position claims must start RESERVED")
        with self.transaction() as connection:
            mode = connection.execute(
                "SELECT mode FROM platform_control WHERE singleton = 1"
            ).fetchone()
            if mode is None or str(mode["mode"]) != PlatformMode.NORMAL.value:
                raise PlatformStateError("position claims require NORMAL platform mode")
            run = connection.execute(
                """
                SELECT state, strategy_id, capital_budget
                FROM platform_runs WHERE run_id = ?
                """,
                (claim.run_id,),
            ).fetchone()
            if run is None or str(run["state"]) != PlatformRunState.RUNNING.value:
                raise PlatformStateError("position claims require a running owner")
            if str(run["strategy_id"]) != claim.strategy_id:
                raise PlatformStateError("position claim strategy does not own the run")
            reserved = sum(
                (
                    Decimal(str(row["margin"]))
                    for row in connection.execute(
                        "SELECT margin FROM position_claims WHERE run_id = ?",
                        (claim.run_id,),
                    ).fetchall()
                ),
                Decimal("0"),
            )
            if reserved + claim.margin > Decimal(str(run["capital_budget"])):
                raise PlatformStateError("position claim exceeds run capital budget")
            aggregate = connection.execute(
                """
                SELECT margin FROM position_claims
                ORDER BY symbol
                """
            ).fetchall()
            total_margin = sum(
                (Decimal(str(row["margin"])) for row in aggregate), Decimal("0")
            )
            if len(aggregate) >= maximum_total_positions:
                raise PlatformStateError(
                    "portfolio position limit was reached concurrently"
                )
            if total_margin + claim.margin > maximum_total_margin:
                raise PlatformStateError(
                    "portfolio margin limit was reached concurrently"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO position_claims(
                        symbol, strategy_id, run_id, position_side, margin,
                        quantity, state, protected, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim.symbol,
                        claim.strategy_id,
                        claim.run_id,
                        claim.position_side.value,
                        str(claim.margin),
                        str(claim.quantity),
                        claim.state.value,
                        int(claim.protected),
                        occurred_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PlatformStateError("symbol already has an active owner") from exc
            self._event(
                connection,
                "POSITION_CLAIMED",
                claim.run_id,
                {"symbol": claim.symbol},
                occurred_at,
            )

    def confirm_position(
        self,
        *,
        run_id: str,
        symbol: str,
        quantity: Decimal,
        protected: bool,
        occurred_at: datetime,
    ) -> None:
        if quantity <= 0:
            raise ValueError("confirmed position quantity must be positive")
        with self.transaction() as connection:
            reservation = connection.execute(
                """
                SELECT quantity FROM position_claims
                WHERE run_id = ? AND symbol = ? AND state = 'RESERVED'
                """,
                (run_id, symbol),
            ).fetchone()
            if reservation is None or Decimal(str(reservation["quantity"])) != quantity:
                raise PlatformStateError(
                    "confirmed position must exactly match the owned reservation"
                )
            cursor = connection.execute(
                """
                UPDATE position_claims
                SET state = 'OPEN', quantity = ?, protected = ?
                WHERE run_id = ? AND symbol = ? AND state = 'RESERVED'
                """,
                (str(quantity), int(protected), run_id, symbol),
            )
            if cursor.rowcount != 1:
                raise PlatformStateError("only an owned reservation can be confirmed")
            self._event(
                connection,
                "POSITION_CONFIRMED",
                run_id,
                {"symbol": symbol, "protected": protected},
                occurred_at,
            )

    def release_empty_reservation(
        self, *, run_id: str, symbol: str, occurred_at: datetime
    ) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM position_claims
                WHERE run_id = ? AND symbol = ? AND state = 'RESERVED'
                """,
                (run_id, symbol),
            )
            if cursor.rowcount != 1:
                raise PlatformStateError("only an empty reservation can be released")
            self._event(
                connection,
                "EMPTY_RESERVATION_RELEASED",
                run_id,
                {"symbol": symbol},
                occurred_at,
            )

    def mark_protected(
        self, *, run_id: str, symbol: str, occurred_at: datetime
    ) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE position_claims SET protected = 1
                WHERE run_id = ? AND symbol = ? AND state = 'OPEN' AND protected = 0
                """,
                (run_id, symbol),
            )
            if cursor.rowcount != 1:
                raise PlatformStateError(
                    "only an unprotected owned position can advance"
                )
            self._event(
                connection,
                "POSITION_PROTECTED",
                run_id,
                {"symbol": symbol},
                occurred_at,
            )

    def settle_claim(self, outcome: ExecutionOutcome) -> None:
        with self.transaction() as connection:
            claim = connection.execute(
                "SELECT run_id, state, quantity FROM position_claims WHERE symbol = ?",
                (outcome.symbol,),
            ).fetchone()
            if (
                claim is None
                or str(claim["run_id"]) != outcome.run_id
                or str(claim["state"]) != ClaimState.OPEN.value
                or Decimal(str(claim["quantity"])) != outcome.quantity
            ):
                raise PlatformStateError("settlement ownership does not match claim")
            connection.execute(
                """
                INSERT INTO execution_outcomes(
                    run_id, symbol, quantity, expected_price, average_price,
                    realized_pnl, commission, funding, closed_at, anomalies_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome.run_id,
                    outcome.symbol,
                    str(outcome.quantity),
                    str(outcome.expected_price),
                    str(outcome.average_price),
                    str(outcome.realized_pnl),
                    str(outcome.commission),
                    str(outcome.funding),
                    outcome.closed_at.isoformat(),
                    json.dumps(outcome.anomalies),
                ),
            )
            connection.execute(
                "DELETE FROM position_claims WHERE symbol = ?", (outcome.symbol,)
            )
            self._event(
                connection,
                "POSITION_SETTLED",
                outcome.run_id,
                {"symbol": outcome.symbol},
                outcome.closed_at,
            )

    def stop(
        self, run_id: str, *, reason: str, occurred_at: datetime
    ) -> PlatformRunStatus:
        if not reason:
            raise ValueError("stop reason is required")
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT state, blocking_reasons_json FROM platform_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None or str(run["state"]) not in {
                PlatformRunState.PLANNED.value,
                PlatformRunState.RUNNING.value,
                PlatformRunState.BLOCKED.value,
                PlatformRunState.BLOCKED_STOP_REQUESTED.value,
                PlatformRunState.STOP_REQUESTED.value,
            }:
                raise PlatformStateError("run cannot be stopped from its current state")
            claim_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM position_claims WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            )
            current_state = PlatformRunState(str(run["state"]))
            if current_state in {
                PlatformRunState.BLOCKED,
                PlatformRunState.BLOCKED_STOP_REQUESTED,
            }:
                state = PlatformRunState.BLOCKED_STOP_REQUESTED
                existing_reasons = tuple(
                    str(item) for item in json.loads(str(run["blocking_reasons_json"]))
                )
            else:
                state = (
                    PlatformRunState.STOP_REQUESTED
                    if claim_count
                    else PlatformRunState.STOPPED
                )
                existing_reasons = ()
            reasons = tuple(dict.fromkeys((*existing_reasons, reason)))
            connection.execute(
                """
                UPDATE platform_runs
                SET state = ?, updated_at = ?, blocking_reasons_json = ?
                WHERE run_id = ?
                """,
                (state.value, occurred_at.isoformat(), json.dumps(reasons), run_id),
            )
            self._event(
                connection,
                (
                    "RUN_STOP_BLOCKED"
                    if state is PlatformRunState.BLOCKED_STOP_REQUESTED
                    else "RUN_STOP_REQUESTED"
                    if claim_count
                    else "RUN_STOPPED"
                ),
                run_id,
                {"reason": reason},
                occurred_at,
            )
        return self.status(run_id)

    def block_all(self, reasons: tuple[str, ...], *, occurred_at: datetime) -> None:
        if not reasons:
            raise ValueError("blocking reasons are required")
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE platform_runs
                SET state = CASE
                        WHEN state IN ('STOP_REQUESTED','BLOCKED_STOP_REQUESTED')
                            THEN 'BLOCKED_STOP_REQUESTED'
                        ELSE 'BLOCKED'
                    END,
                    updated_at = ?, blocking_reasons_json = ?
                WHERE state IN (
                    'RUNNING','STOP_REQUESTED','BLOCKED','BLOCKED_STOP_REQUESTED'
                )
                """,
                (occurred_at.isoformat(), json.dumps(reasons)),
            )
            self._event(
                connection,
                "PLATFORM_RECONCILIATION_BLOCKED",
                None,
                {"reasons": reasons},
                occurred_at,
            )

    def reconciliation_passed(self, *, occurred_at: datetime) -> None:
        with self.transaction() as connection:
            blocked = connection.execute(
                """
                SELECT run_id, state FROM platform_runs
                WHERE state IN ('BLOCKED','BLOCKED_STOP_REQUESTED')
                """
            ).fetchall()
            for row in blocked:
                run_id = str(row["run_id"])
                claim_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM position_claims WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()[0]
                )
                if str(row["state"]) == PlatformRunState.BLOCKED_STOP_REQUESTED.value:
                    next_state = (
                        PlatformRunState.STOP_REQUESTED
                        if claim_count
                        else PlatformRunState.STOPPED
                    )
                else:
                    next_state = (
                        PlatformRunState.RUNNING
                        if claim_count
                        else PlatformRunState.STOPPED
                    )
                connection.execute(
                    """
                    UPDATE platform_runs
                    SET state = ?, updated_at = ?, blocking_reasons_json = '[]'
                    WHERE run_id = ?
                    """,
                    (next_state.value, occurred_at.isoformat(), run_id),
                )
            self._event(
                connection,
                "PLATFORM_RECONCILIATION_PASSED",
                None,
                {"recovered_runs": len(blocked)},
                occurred_at,
            )

    def outcomes(self, run_id: str) -> tuple[ExecutionOutcome, ...]:
        with self.read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_outcomes WHERE run_id = ? ORDER BY outcome_id",
                (run_id,),
            ).fetchall()
        return tuple(
            ExecutionOutcome(
                run_id=str(row["run_id"]),
                symbol=str(row["symbol"]),
                quantity=Decimal(str(row["quantity"])),
                expected_price=Decimal(str(row["expected_price"])),
                average_price=Decimal(str(row["average_price"])),
                realized_pnl=Decimal(str(row["realized_pnl"])),
                commission=Decimal(str(row["commission"])),
                funding=Decimal(str(row["funding"])),
                closed_at=datetime.fromisoformat(str(row["closed_at"])),
                anomalies=tuple(json.loads(str(row["anomalies_json"]))),
            )
            for row in rows
        )

    @staticmethod
    def _claim(row: sqlite3.Row) -> PositionClaim:
        return PositionClaim(
            strategy_id=str(row["strategy_id"]),
            run_id=str(row["run_id"]),
            symbol=str(row["symbol"]),
            position_side=PositionSide(str(row["position_side"])),
            margin=Decimal(str(row["margin"])),
            quantity=Decimal(str(row["quantity"])),
            state=ClaimState(str(row["state"])),
            protected=bool(row["protected"]),
            claimed_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        event_type: str,
        run_id: str | None,
        payload: Mapping[str, Any],
        occurred_at: datetime,
    ) -> None:
        if occurred_at.tzinfo is None:
            raise ValueError("platform event time must be timezone-aware")
        connection.execute(
            """
            INSERT INTO platform_events(event_type, run_id, payload_json, occurred_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                event_type,
                run_id,
                json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                occurred_at.isoformat(),
            ),
        )
