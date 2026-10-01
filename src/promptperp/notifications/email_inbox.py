from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class QueryReply:
    reply_id: str
    recipient_email: str
    payload_text: str
    attempt_count: int


class SQLiteInboxRegistry:
    """Isolated dedupe/rate-limit state; it has no accounting write access."""

    def __init__(self, path: str | Path, *, hourly_limit: int = 5):
        if hourly_limit <= 0:
            raise ValueError("hourly limit must be positive")
        self.path = Path(path)
        self.hourly_limit = hourly_limit

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS accepted_messages (
                    message_hash TEXT PRIMARY KEY,
                    sender_hash TEXT NOT NULL,
                    received_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS inbox_sender_time_idx ON accepted_messages(sender_hash, received_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS query_replies (
                    reply_id TEXT PRIMARY KEY,
                    source_message_hash TEXT NOT NULL UNIQUE,
                    recipient_email TEXT NOT NULL,
                    payload_text TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('PENDING', 'SENDING', 'SENT')
                    ),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    delivery_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        os.chmod(self.path, 0o600)

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def accept(self, *, message_id: str, sender: str, received_at: datetime) -> bool:
        if not message_id or not sender or received_at.tzinfo is None:
            return False
        message_hash = self._hash(message_id)
        sender_hash = self._hash(sender.strip().casefold())
        received_utc = received_at.astimezone(timezone.utc)
        cutoff = received_utc - timedelta(hours=1)
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT 1 FROM accepted_messages WHERE message_hash = ?",
                (message_hash,),
            ).fetchone()
            if duplicate is not None:
                connection.execute("ROLLBACK")
                return False
            count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM accepted_messages
                    WHERE sender_hash = ? AND received_at > ?
                    """,
                    (sender_hash, cutoff.isoformat()),
                ).fetchone()[0]
            )
            if count >= self.hourly_limit:
                connection.execute("ROLLBACK")
                return False
            connection.execute(
                "INSERT INTO accepted_messages(message_hash, sender_hash, received_at) VALUES (?, ?, ?)",
                (message_hash, sender_hash, received_utc.isoformat()),
            )
            connection.execute("COMMIT")
            return True
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def enqueue_reply(
        self,
        *,
        message_id: str,
        recipient_email: str,
        payload_text: str,
        occurred_at: datetime,
    ) -> QueryReply:
        message_hash = self._hash(message_id)
        reply_id = f"query-reply:{message_hash[:32]}"
        recipient = recipient_email.strip().casefold()
        if "@" not in recipient or not payload_text or occurred_at.tzinfo is None:
            raise ValueError("query reply is malformed")
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            existing = connection.execute(
                "SELECT * FROM query_replies WHERE reply_id = ?", (reply_id,)
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["source_message_hash"]) != message_hash
                    or str(existing["recipient_email"]) != recipient
                    or str(existing["payload_text"]) != payload_text
                ):
                    raise ValueError("query reply identity was reused")
                return self._reply(existing)
            connection.execute(
                """
                INSERT INTO query_replies(
                    reply_id, source_message_hash, recipient_email, payload_text,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    reply_id,
                    message_hash,
                    recipient,
                    payload_text,
                    occurred_at.isoformat(),
                    occurred_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM query_replies WHERE reply_id = ?", (reply_id,)
            ).fetchone()
        assert row is not None
        return self._reply(row)

    def pending_replies(self) -> tuple[QueryReply, ...]:
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM query_replies WHERE status = 'PENDING'
                ORDER BY created_at, reply_id
                """
            ).fetchall()
        return tuple(self._reply(row) for row in rows)

    def claim_reply(
        self, *, reply_id: str, delivery_id: str, occurred_at: datetime
    ) -> QueryReply:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE query_replies
                SET status = 'SENDING', attempt_count = attempt_count + 1,
                    delivery_id = ?, updated_at = ?
                WHERE reply_id = ? AND status = 'PENDING'
                """,
                (delivery_id, occurred_at.isoformat(), reply_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("query reply is not safely claimable")
            row = connection.execute(
                "SELECT * FROM query_replies WHERE reply_id = ?", (reply_id,)
            ).fetchone()
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        assert row is not None
        return self._reply(row)

    def mark_reply_sent(
        self, *, reply_id: str, delivery_id: str, occurred_at: datetime
    ) -> None:
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute(
                """
                UPDATE query_replies SET status = 'SENT', updated_at = ?
                WHERE reply_id = ? AND status = 'SENDING' AND delivery_id = ?
                """,
                (occurred_at.isoformat(), reply_id, delivery_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("query reply delivery identity does not match")

    @staticmethod
    def _reply(row: sqlite3.Row) -> QueryReply:
        return QueryReply(
            reply_id=str(row["reply_id"]),
            recipient_email=str(row["recipient_email"]),
            payload_text=str(row["payload_text"]),
            attempt_count=int(row["attempt_count"]),
        )
