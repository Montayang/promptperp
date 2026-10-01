from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from promptperp.accounting.errors import AccountingValidationError
from promptperp.accounting.models import InvestorView
from promptperp.reporting.models import InvestorStatement, OutboxMessage
from promptperp.storage.accounting_sqlite import SQLiteAccountingStore


class SQLiteReportingStore:
    def __init__(self, accounting_store: SQLiteAccountingStore):
        self.accounting_store = accounting_store

    def enqueue_statement(
        self, statement: InvestorStatement, payload_text: str
    ) -> OutboxMessage:
        payload_json = json.dumps(
            statement.payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        message_id = f"email:{statement.statement_id}"
        with self.accounting_store.transaction() as connection:
            recipient = connection.execute(
                """
                SELECT email FROM verified_email_addresses
                WHERE investor_id = ? AND verified = 1
                """,
                (statement.investor_id,),
            ).fetchone()
            snapshot = connection.execute(
                """
                SELECT snapshot_id FROM valuation_snapshots
                WHERE pool_id = ? AND reconciliation_id = ?
                """,
                (statement.pool_id, statement.reconciliation_id),
            ).fetchone()
            if recipient is None or snapshot is None:
                raise AccountingValidationError(
                    "statement needs verified recipient and reconciled snapshot"
                )
            existing = connection.execute(
                "SELECT payload_json FROM statements WHERE statement_id = ?",
                (statement.statement_id,),
            ).fetchone()
            if existing is not None and str(existing["payload_json"]) != payload_json:
                raise AccountingValidationError(
                    "statement identity was reused with different content"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO statements(
                    statement_id, investor_id, period_start, period_end,
                    snapshot_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    statement.statement_id,
                    statement.investor_id,
                    statement.period.start_at.isoformat(),
                    statement.period.end_at.isoformat(),
                    snapshot["snapshot_id"],
                    payload_json,
                    statement.created_at.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO email_outbox_messages(
                    message_id, statement_id, recipient_email, payload_text,
                    status, attempt_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?)
                """,
                (
                    message_id,
                    statement.statement_id,
                    recipient["email"],
                    payload_text,
                    statement.created_at.isoformat(),
                    statement.created_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM email_outbox_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        if row is None:
            raise AccountingValidationError("failed to create email outbox message")
        return self._outbox(row)

    def pending_messages(self) -> tuple[OutboxMessage, ...]:
        with self.accounting_store.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM email_outbox_messages
                WHERE status IN ('PENDING', 'FAILED')
                ORDER BY created_at, message_id
                """
            ).fetchall()
        return tuple(self._outbox(row) for row in rows)

    def enabled_report_preferences(self) -> tuple[sqlite3.Row, ...]:
        with self.accounting_store.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT p.*, s.next_due_at, s.last_due_at, s.last_statement_id
                FROM report_preferences AS p
                JOIN investors AS i ON i.investor_id = p.investor_id
                LEFT JOIN report_schedule_state AS s
                  ON s.investor_id = p.investor_id
                WHERE p.enabled = 1 AND i.active = 1
                ORDER BY p.investor_id
                """
            ).fetchall()
        return tuple(rows)

    def initialize_schedule(
        self, *, investor_id: str, next_due_at: datetime, occurred_at: datetime
    ) -> None:
        with self.accounting_store.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO report_schedule_state(
                    investor_id, next_due_at, updated_at
                ) VALUES (?, ?, ?)
                """,
                (
                    investor_id,
                    next_due_at.isoformat(),
                    occurred_at.isoformat(),
                ),
            )

    def advance_schedule(
        self,
        *,
        investor_id: str,
        expected_due_at: datetime,
        next_due_at: datetime,
        statement_id: str,
        occurred_at: datetime,
    ) -> bool:
        with self.accounting_store.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE report_schedule_state
                SET next_due_at = ?, last_due_at = ?, last_statement_id = ?,
                    updated_at = ?
                WHERE investor_id = ? AND next_due_at = ?
                """,
                (
                    next_due_at.isoformat(),
                    expected_due_at.isoformat(),
                    statement_id,
                    occurred_at.isoformat(),
                    investor_id,
                    expected_due_at.isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def operational_counts(self) -> dict[str, Any]:
        with self.accounting_store.read_connection() as connection:
            outbox = connection.execute(
                """
                SELECT status, COUNT(*) AS count,
                       MIN(created_at) AS oldest_created_at
                FROM email_outbox_messages GROUP BY status
                """
            ).fetchall()
            schedules = connection.execute(
                """
                SELECT COUNT(*) AS enabled,
                       SUM(CASE WHEN s.investor_id IS NULL THEN 1 ELSE 0 END)
                           AS uninitialized
                FROM report_preferences AS p
                JOIN investors AS i ON i.investor_id = p.investor_id
                LEFT JOIN report_schedule_state AS s
                  ON s.investor_id = p.investor_id
                WHERE p.enabled = 1 AND i.active = 1
                """
            ).fetchone()
        return {
            "outbox": {
                str(row["status"]): {
                    "count": int(row["count"]),
                    "oldest_created_at": row["oldest_created_at"],
                }
                for row in outbox
            },
            "enabled_schedules": int(schedules["enabled"] or 0),
            "uninitialized_schedules": int(schedules["uninitialized"] or 0),
        }

    def claim_message(
        self,
        message_id: str,
        *,
        delivery_id: str,
        occurred_at: datetime,
    ) -> OutboxMessage:
        if not delivery_id.strip():
            raise AccountingValidationError("delivery_id is required")
        with self.accounting_store.transaction() as connection:
            row = connection.execute(
                """
                SELECT o.* FROM email_outbox_messages AS o
                JOIN verified_email_addresses AS e
                  ON e.email = o.recipient_email
                WHERE o.message_id = ? AND e.verified = 1
                """,
                (message_id,),
            ).fetchone()
            if row is None:
                raise AccountingValidationError(
                    "outbox message has no verified recipient"
                )
            if str(row["status"]) not in {"PENDING", "FAILED"}:
                raise AccountingValidationError(
                    "outbox message is already sent or delivery is uncertain"
                )
            cursor = connection.execute(
                """
                UPDATE email_outbox_messages
                SET status = 'SENDING', attempt_count = attempt_count + 1,
                    delivery_id = ?, claimed_at = ?, updated_at = ?
                WHERE message_id = ? AND status IN ('PENDING', 'FAILED')
                """,
                (
                    delivery_id,
                    occurred_at.isoformat(),
                    occurred_at.isoformat(),
                    message_id,
                ),
            )
            if cursor.rowcount != 1:
                raise AccountingValidationError("outbox message could not be claimed")
            claimed = connection.execute(
                "SELECT * FROM email_outbox_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        if claimed is None:
            raise AccountingValidationError("claimed outbox message is missing")
        return self._outbox(claimed)

    def mark_sent(
        self,
        message_id: str,
        *,
        delivery_id: str,
        occurred_at: datetime,
    ) -> None:
        with self.accounting_store.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE email_outbox_messages
                SET status = 'SENT', updated_at = ?
                WHERE message_id = ? AND status = 'SENDING' AND delivery_id = ?
                """,
                (occurred_at.isoformat(), message_id, delivery_id),
            )
            if cursor.rowcount != 1:
                raise AccountingValidationError(
                    "claimed outbox message or delivery identity does not match"
                )

    @staticmethod
    def _outbox(row: sqlite3.Row) -> OutboxMessage:
        return OutboxMessage(
            message_id=str(row["message_id"]),
            statement_id=str(row["statement_id"]),
            recipient_email=str(row["recipient_email"]),
            payload_text=str(row["payload_text"]),
            attempt_count=int(row["attempt_count"]),
        )


class SQLiteInvestorViewReader:
    """Narrow read-only adapter for the inbound email-query process."""

    def __init__(self, accounting_store: SQLiteAccountingStore):
        self._accounting_store = accounting_store

    def investor_view_for_email(self, email: str) -> InvestorView | None:
        investor_id = self._accounting_store.investor_id_for_email(email)
        if investor_id is None:
            return None
        return self._accounting_store.investor_view_by_id(investor_id)
