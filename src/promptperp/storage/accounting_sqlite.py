from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence, cast

from promptperp.accounting import chart
from promptperp.accounting.errors import (
    AccountingValidationError,
    DuplicateEventConflict,
    LedgerIntegrityError,
)
from promptperp.accounting.models import (
    BASE_ASSET,
    MONEY_QUANTUM,
    EntryDirection,
    InvestorView,
    LedgerTransaction,
    Posting,
    ReconciliationResult,
    ReportFrequency,
    SourceEvent,
    ValuationSnapshot,
)

SCHEMA_VERSION = 5
GENESIS_HASH = "0" * 64


def _decimal(value: str | int | float | Decimal) -> Decimal:
    return Decimal(str(value))


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_event_json(event: SourceEvent) -> str:
    return _canonical_json(
        {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "schema_version": event.schema_version,
            "occurred_at": event.occurred_at.isoformat(),
            "payload": dict(event.payload),
        }
    )


class SQLiteAccountingStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = self._connect()
        try:
            migration_root = resources.files("promptperp.storage.migrations")
            for version, filename in (
                (1, "0001_accounting.sql"),
                (2, "0002_shadow_events.sql"),
                (3, "0003_multi_pool_bootstrap.sql"),
                (4, "0004_email_delivery.sql"),
                (5, "0005_reporting_operations.sql"),
            ):
                migration_table_exists = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'schema_migrations'
                    """
                ).fetchone()
                if migration_table_exists is not None:
                    applied = connection.execute(
                        "SELECT 1 FROM schema_migrations WHERE version = ?",
                        (version,),
                    ).fetchone()
                    if applied is not None:
                        continue
                script = migration_root.joinpath(filename).read_text(encoding="utf-8")
                applied_at = datetime.now(timezone.utc).isoformat().replace("'", "''")
                try:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        + script
                        + "\nINSERT INTO schema_migrations(version, applied_at) "
                        + f"VALUES ({version}, '{applied_at}');\nCOMMIT;"
                    )
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            if [int(row[0]) for row in versions] != list(range(1, SCHEMA_VERSION + 1)):
                raise LedgerIntegrityError("unsupported accounting schema version")
        finally:
            connection.close()
        os.chmod(self.path, 0o600)

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(
                f"file:{self.path.resolve()}?mode=ro",
                uri=True,
                timeout=5,
                isolation_level=None,
            )
            connection.execute("PRAGMA query_only = ON")
        else:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
            if mode.lower() != "wal":
                connection.close()
                raise LedgerIntegrityError("SQLite WAL mode is required")
            connection.execute("PRAGMA synchronous = FULL")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            connection.close()
            raise LedgerIntegrityError("SQLite foreign keys are required")
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
        connection = self._connect(read_only=True)
        try:
            yield connection
        finally:
            connection.close()

    def register_investor(
        self,
        *,
        investor_id: str,
        display_name: str,
        email: str,
        frequency: ReportFrequency,
        timezone_name: str,
        local_send_time: str,
        monthly_send_day: int,
        occurred_at: datetime,
    ) -> None:
        normalized_email = email.strip().casefold()
        if not investor_id or not display_name or "@" not in normalized_email:
            raise AccountingValidationError("invalid investor identity or email")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO investors(investor_id, display_name, created_at) VALUES (?, ?, ?)",
                (investor_id, display_name, occurred_at.isoformat()),
            )
            connection.execute(
                "INSERT INTO verified_email_addresses(email, investor_id, verified, created_at) VALUES (?, ?, 1, ?)",
                (normalized_email, investor_id, occurred_at.isoformat()),
            )
            connection.execute(
                """
                INSERT INTO report_preferences(
                    investor_id, frequency, timezone, local_send_time,
                    monthly_send_day, enabled
                ) VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    investor_id,
                    frequency.value,
                    timezone_name,
                    local_send_time,
                    monthly_send_day,
                ),
            )

    def create_pool(
        self,
        *,
        pool_id: str,
        strategy_id: str,
        strategy_version: str,
        initial_unit_nav: Decimal,
        occurred_at: datetime,
    ) -> None:
        if initial_unit_nav <= 0 or not initial_unit_nav.is_finite():
            raise AccountingValidationError("initial unit NAV must be positive")
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO strategy_pools(
                    pool_id, strategy_id, strategy_version, base_asset,
                    initial_unit_nav, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    pool_id,
                    strategy_id,
                    strategy_version,
                    BASE_ASSET,
                    str(initial_unit_nav),
                    occurred_at.isoformat(),
                ),
            )

    def get_pool(self, pool_id: str) -> sqlite3.Row:
        with self._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM strategy_pools WHERE pool_id = ? AND active = 1",
                (pool_id,),
            ).fetchone()
        if row is None:
            raise AccountingValidationError("active strategy pool does not exist")
        return cast(sqlite3.Row, row)

    def get_pool_for_strategy(self, strategy_id: str) -> sqlite3.Row:
        with self._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM strategy_pools WHERE strategy_id = ? AND active = 1",
                (strategy_id,),
            ).fetchone()
        if row is None:
            raise AccountingValidationError("settlement strategy has no active pool")
        return cast(sqlite3.Row, row)

    def event_result(self, event: SourceEvent) -> Mapping[str, str] | None:
        with self._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT payload_hash, result_json FROM source_events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
        if row is None:
            return None
        if str(row["payload_hash"]) != _sha256(_source_event_json(event)):
            raise DuplicateEventConflict("event ID was reused with different payload")
        if row["result_json"] is None:
            raise LedgerIntegrityError("source event exists without a committed result")
        value = json.loads(str(row["result_json"]))
        if not isinstance(value, dict):
            raise LedgerIntegrityError("source event result is invalid")
        return {str(key): str(item) for key, item in value.items()}

    def insert_source_event(
        self, connection: sqlite3.Connection, event: SourceEvent
    ) -> None:
        payload_json = _canonical_json(dict(event.payload))
        connection.execute(
            """
            INSERT INTO source_events(
                event_id, event_type, schema_version, occurred_at,
                payload_json, payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.event_type,
                event.schema_version,
                event.occurred_at.isoformat(),
                payload_json,
                _sha256(_source_event_json(event)),
            ),
        )

    def complete_source_event(
        self,
        connection: sqlite3.Connection,
        event_id: str,
        result: Mapping[str, str],
        processed_at: datetime,
    ) -> None:
        connection.execute(
            "UPDATE source_events SET result_json = ?, processed_at = ? WHERE event_id = ?",
            (_canonical_json(dict(result)), processed_at.isoformat(), event_id),
        )

    def insert_transaction(
        self,
        connection: sqlite3.Connection,
        source_event_id: str,
        transaction: LedgerTransaction,
    ) -> None:
        row = connection.execute(
            "SELECT record_hash FROM ledger_transactions ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = GENESIS_HASH if row is None else str(row["record_hash"])
        source_row = connection.execute(
            "SELECT payload_hash FROM source_events WHERE event_id = ?",
            (source_event_id,),
        ).fetchone()
        if source_row is None:
            raise LedgerIntegrityError("ledger transaction source event is missing")
        payload = self._transaction_payload(
            transaction,
            previous_hash,
            source_event_id,
            str(source_row["payload_hash"]),
        )
        record_hash = _sha256(_canonical_json(payload))
        connection.execute(
            """
            INSERT INTO ledger_transactions(
                transaction_id, source_event_id, kind, occurred_at, actor,
                reason, external_reference, previous_hash, record_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transaction.transaction_id,
                source_event_id,
                transaction.kind,
                transaction.occurred_at.isoformat(),
                transaction.actor,
                transaction.reason,
                transaction.external_reference,
                previous_hash,
                record_hash,
            ),
        )
        connection.executemany(
            """
            INSERT INTO ledger_entries(
                transaction_id, posting_index, account, direction, amount, asset
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    transaction.transaction_id,
                    index,
                    posting.account,
                    posting.direction.value,
                    str(posting.amount),
                    posting.asset,
                )
                for index, posting in enumerate(transaction.postings)
            ],
        )

    @staticmethod
    def _transaction_payload(
        transaction: LedgerTransaction,
        previous_hash: str,
        source_event_id: str,
        source_event_hash: str,
    ) -> dict[str, Any]:
        return {
            "transaction_id": transaction.transaction_id,
            "kind": transaction.kind,
            "occurred_at": transaction.occurred_at.isoformat(),
            "actor": transaction.actor,
            "reason": transaction.reason,
            "external_reference": transaction.external_reference,
            "previous_hash": previous_hash,
            "source_event_id": source_event_id,
            "source_event_hash": source_event_hash,
            "postings": [
                {
                    "account": item.account,
                    "direction": item.direction.value,
                    "amount": str(item.amount),
                    "asset": item.asset,
                }
                for item in transaction.postings
            ],
        }

    def pool_cash(self, connection: sqlite3.Connection, pool_id: str) -> Decimal:
        rows = connection.execute(
            """
            SELECT direction, amount
            FROM ledger_entries WHERE account = ?
            """,
            (chart.pool_cash(pool_id),),
        ).fetchall()
        return sum(
            (
                _decimal(row["amount"])
                if row["direction"] == EntryDirection.DEBIT.value
                else -_decimal(row["amount"])
                for row in rows
            ),
            Decimal("0"),
        )

    def pool_units(self, connection: sqlite3.Connection, pool_id: str) -> Decimal:
        rows = connection.execute(
            "SELECT units FROM subscriptions WHERE pool_id = ? AND active = 1",
            (pool_id,),
        ).fetchall()
        return sum((_decimal(row["units"]) for row in rows), Decimal("0"))

    def latest_snapshot(
        self, connection: sqlite3.Connection, pool_id: str
    ) -> sqlite3.Row | None:
        row = connection.execute(
            """
            SELECT * FROM valuation_snapshots
            WHERE pool_id = ?
            ORDER BY datetime(occurred_at) DESC, snapshot_id DESC LIMIT 1
            """,
            (pool_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def insert_reconciliation(
        self,
        connection: sqlite3.Connection,
        result: ReconciliationResult,
    ) -> None:
        connection.execute(
            """
            INSERT INTO reconciliations(
                reconciliation_id, occurred_at, exchange_equity, internal_equity,
                difference, status, reasons_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.reconciliation_id,
                result.occurred_at.isoformat(),
                str(result.exchange_equity),
                str(result.internal_equity),
                str(result.difference),
                result.status.value,
                json.dumps(result.reasons, separators=(",", ":")),
            ),
        )

    def insert_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot: ValuationSnapshot,
    ) -> None:
        connection.execute(
            """
            INSERT INTO valuation_snapshots(
                snapshot_id, pool_id, occurred_at, pool_equity,
                outstanding_units, unit_nav, reconciliation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.snapshot_id,
                snapshot.pool_id,
                snapshot.occurred_at.isoformat(),
                str(snapshot.pool_equity),
                str(snapshot.outstanding_units),
                str(snapshot.unit_nav),
                snapshot.reconciliation_id,
            ),
        )

    def investor_view_by_id(self, investor_id: str) -> InvestorView:
        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT i.investor_id, i.display_name, s.pool_id, s.units,
                       s.total_contributions, s.total_withdrawals,
                       p.strategy_id, p.strategy_version,
                       v.unit_nav, v.occurred_at, v.reconciliation_id
                FROM investors i
                JOIN subscriptions s ON s.investor_id = i.investor_id AND s.active = 1
                JOIN strategy_pools p ON p.pool_id = s.pool_id
                JOIN valuation_snapshots v ON v.snapshot_id = (
                    SELECT snapshot_id FROM valuation_snapshots
                    WHERE pool_id = s.pool_id
                    ORDER BY datetime(occurred_at) DESC, snapshot_id DESC LIMIT 1
                )
                WHERE i.investor_id = ? AND i.active = 1
                """,
                (investor_id,),
            ).fetchall()
        if not rows:
            raise AccountingValidationError("investor has no reconciled valuation")
        if len(rows) != 1:
            raise AccountingValidationError(
                "multi-pool investor reporting requires a portfolio statement"
            )
        row = rows[0]
        units = _decimal(row["units"])
        nav = _decimal(row["unit_nav"])
        equity = (units * nav).quantize(MONEY_QUANTUM)
        contributions = _decimal(row["total_contributions"])
        withdrawals = _decimal(row["total_withdrawals"])
        return_rate = (equity + withdrawals - contributions) / contributions
        return InvestorView(
            investor_id=str(row["investor_id"]),
            display_name=str(row["display_name"]),
            pool_id=str(row["pool_id"]),
            strategy_id=str(row["strategy_id"]),
            strategy_version=str(row["strategy_version"]),
            units=units,
            unit_nav=nav,
            equity=equity,
            net_contributions=contributions - withdrawals,
            return_rate=return_rate,
            snapshot_at=datetime.fromisoformat(str(row["occurred_at"])),
            reconciliation_id=str(row["reconciliation_id"]),
        )

    def investor_id_for_email(self, email: str) -> str | None:
        with self._connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT investor_id FROM verified_email_addresses
                WHERE email = ? AND verified = 1
                """,
                (email.strip().casefold(),),
            ).fetchone()
        return None if row is None else str(row["investor_id"])

    def audit_ledger(self) -> int:
        with self._connect(read_only=True) as connection:
            transactions = connection.execute(
                "SELECT * FROM ledger_transactions ORDER BY sequence"
            ).fetchall()
            previous_hash = GENESIS_HASH
            for row in transactions:
                entries = connection.execute(
                    "SELECT * FROM ledger_entries WHERE transaction_id = ? ORDER BY posting_index",
                    (row["transaction_id"],),
                ).fetchall()
                try:
                    transaction = LedgerTransaction(
                        transaction_id=str(row["transaction_id"]),
                        kind=str(row["kind"]),
                        occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
                        actor=str(row["actor"]),
                        reason=str(row["reason"]),
                        external_reference=str(row["external_reference"]),
                        postings=tuple(
                            Posting(
                                account=str(entry["account"]),
                                direction=EntryDirection(str(entry["direction"])),
                                amount=_decimal(entry["amount"]),
                                asset=str(entry["asset"]),
                            )
                            for entry in entries
                        ),
                    )
                except (ValueError, AccountingValidationError) as exc:
                    raise LedgerIntegrityError(
                        "ledger transaction or postings were modified"
                    ) from exc
                if str(row["previous_hash"]) != previous_hash:
                    raise LedgerIntegrityError("ledger hash chain is broken")
                source = connection.execute(
                    "SELECT * FROM source_events WHERE event_id = ?",
                    (row["source_event_id"],),
                ).fetchone()
                if source is None or source["result_json"] is None:
                    raise LedgerIntegrityError(
                        "ledger source event is missing or incomplete"
                    )
                try:
                    source_payload = json.loads(str(source["payload_json"]))
                    source_json = _canonical_json(
                        {
                            "event_id": str(source["event_id"]),
                            "event_type": str(source["event_type"]),
                            "schema_version": int(source["schema_version"]),
                            "occurred_at": str(source["occurred_at"]),
                            "payload": source_payload,
                        }
                    )
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise LedgerIntegrityError("source event was modified") from exc
                source_hash = _sha256(source_json)
                if str(source["payload_hash"]) != source_hash:
                    raise LedgerIntegrityError("source event was modified")
                expected = _sha256(
                    _canonical_json(
                        self._transaction_payload(
                            transaction,
                            previous_hash,
                            str(row["source_event_id"]),
                            source_hash,
                        )
                    )
                )
                if str(row["record_hash"]) != expected:
                    raise LedgerIntegrityError("ledger transaction was modified")
                previous_hash = expected
        return len(transactions)

    def integrity_check(self) -> None:
        with self._connect(read_only=True) as connection:
            result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if result != "ok":
            raise LedgerIntegrityError(f"SQLite integrity check failed: {result}")

    def backup(self, destination: str | Path) -> None:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        source_connection = self._connect(read_only=True)
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
        os.chmod(target, 0o600)

    def operational_fingerprint(self) -> Mapping[str, Any]:
        """Return non-financial identities and counts for backup comparison."""

        tables = (
            "investors",
            "strategy_pools",
            "subscriptions",
            "source_events",
            "ledger_transactions",
            "reconciliations",
            "valuation_snapshots",
            "statements",
            "email_outbox_messages",
        )
        with self._connect(read_only=True) as connection:
            counts = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in tables
            }
            migration = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
            )
            ledger_head = connection.execute(
                "SELECT record_hash FROM ledger_transactions ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            reconciliation = connection.execute(
                """
                SELECT reconciliation_id, occurred_at, status
                FROM reconciliations
                ORDER BY datetime(occurred_at) DESC, reconciliation_id DESC LIMIT 1
                """
            ).fetchone()
        return {
            "schema_version": migration,
            "counts": counts,
            "ledger_head_hash": None if ledger_head is None else str(ledger_head[0]),
            "latest_reconciliation": (
                None
                if reconciliation is None
                else {
                    "reconciliation_id": str(reconciliation["reconciliation_id"]),
                    "occurred_at": str(reconciliation["occurred_at"]),
                    "status": str(reconciliation["status"]),
                }
            ),
        }

    def list_active_pools(
        self, connection: sqlite3.Connection
    ) -> Sequence[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM strategy_pools WHERE active = 1 ORDER BY pool_id"
        ).fetchall()
