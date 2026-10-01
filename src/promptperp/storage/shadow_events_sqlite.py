from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Mapping, cast

from promptperp.accounting.errors import DuplicateEventConflict
from promptperp.accounting.exchange_events import OwnedSettlementTotals
from promptperp.storage.accounting_sqlite import SQLiteAccountingStore

if TYPE_CHECKING:
    from promptperp.execution.models import TradeIntent


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class SQLiteShadowEventStore:
    """Ownership registry and quarantine sharing the accounting transaction DB."""

    def __init__(self, accounting_store: SQLiteAccountingStore):
        self.accounting_store = accounting_store

    def capture_boundary(self, *, initialized_at: datetime) -> datetime:
        """Return the immutable start boundary, creating it on first deployment."""

        with self.accounting_store.transaction() as connection:
            row = connection.execute(
                "SELECT value FROM shadow_metadata WHERE key = 'capture_started_at'"
            ).fetchone()
            if row is None:
                value = initialized_at.isoformat()
                connection.execute(
                    "INSERT INTO shadow_metadata(key, value) VALUES ('capture_started_at', ?)",
                    (value,),
                )
            else:
                value = str(row["value"])
        boundary = datetime.fromisoformat(value)
        if boundary.tzinfo is None:
            raise ValueError("shadow capture boundary must be timezone-aware")
        return boundary

    def ledger_posting_boundary(self, *, initialized_at: datetime) -> datetime:
        """Enable live posting without back-posting pre-cutover shadow events."""

        with self.accounting_store.transaction() as connection:
            row = connection.execute(
                "SELECT value FROM shadow_metadata WHERE key = 'ledger_posting_started_at'"
            ).fetchone()
            if row is None:
                pending = connection.execute(
                    """
                    SELECT COUNT(*) FROM shadow_exchange_events
                    WHERE status = 'VERIFIED_PENDING_LEDGER'
                    """
                ).fetchone()[0]
                if int(pending) != 0:
                    raise ValueError(
                        "cannot enable ledger posting with pre-cutover pending events"
                    )
                value = initialized_at.isoformat()
                connection.execute(
                    "INSERT INTO shadow_metadata(key, value) VALUES ('ledger_posting_started_at', ?)",
                    (value,),
                )
            else:
                value = str(row["value"])
        boundary = datetime.fromisoformat(value)
        if boundary.tzinfo is None:
            raise ValueError("ledger posting boundary must be timezone-aware")
        return boundary

    def begin_intent(self, intent: TradeIntent, *, opened_at: datetime) -> None:
        payload = (
            intent.strategy_id,
            intent.run_id,
            intent.symbol,
            opened_at.isoformat(),
        )
        with self.accounting_store.transaction() as connection:
            existing = connection.execute(
                "SELECT strategy_id, run_id, symbol, opened_at FROM execution_intents WHERE intent_id = ?",
                (intent.intent_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != payload:
                    raise DuplicateEventConflict(
                        "execution intent ID was reused with different ownership"
                    )
                return
            connection.execute(
                """
                INSERT INTO execution_intents(
                    intent_id, strategy_id, run_id, symbol, opened_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (intent.intent_id, *payload),
            )

    def prepare_order(
        self,
        *,
        intent_id: str,
        role: str,
        client_order_id: str,
        is_algo: bool,
        created_at: datetime,
    ) -> None:
        payload = (intent_id, role, int(is_algo), created_at.isoformat())
        with self.accounting_store.transaction() as connection:
            existing = connection.execute(
                """
                SELECT intent_id, role, is_algo, created_at
                FROM owned_exchange_orders WHERE client_order_id = ?
                """,
                (client_order_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != payload:
                    raise DuplicateEventConflict(
                        "client order ID was reused with different ownership"
                    )
                return
            connection.execute(
                """
                INSERT INTO owned_exchange_orders(
                    client_order_id, intent_id, role, is_algo, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (client_order_id, *payload),
            )

    def confirm_order(self, *, client_order_id: str, exchange_order_id: str) -> None:
        if not exchange_order_id:
            raise ValueError("exchange order identity is required")
        with self.accounting_store.transaction() as connection:
            row = connection.execute(
                "SELECT is_algo, algo_id, exchange_order_id FROM owned_exchange_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
            if row is None:
                raise DuplicateEventConflict("order confirmation has no prepared owner")
            field = "algo_id" if int(row["is_algo"]) else "exchange_order_id"
            current = row[field]
            if current is not None and str(current) != exchange_order_id:
                raise DuplicateEventConflict(
                    "client order ID resolved to conflicting exchange orders"
                )
            connection.execute(
                f"UPDATE owned_exchange_orders SET {field} = ? WHERE client_order_id = ?",
                (exchange_order_id, client_order_id),
            )

    def close_intent(self, intent_id: str, *, closed_at: datetime) -> None:
        with self.accounting_store.transaction() as connection:
            row = connection.execute(
                "SELECT opened_at, closed_at FROM execution_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise DuplicateEventConflict("cannot close an unknown execution intent")
            if closed_at < datetime.fromisoformat(str(row["opened_at"])):
                raise ValueError("execution close precedes its open time")
            current = row["closed_at"]
            if current is not None and str(current) != closed_at.isoformat():
                raise DuplicateEventConflict("execution intent has a conflicting close")
            connection.execute(
                "UPDATE execution_intents SET closed_at = ? WHERE intent_id = ?",
                (closed_at.isoformat(), intent_id),
            )

    def confirm_algo_trigger(
        self, *, client_order_id: str, exchange_order_id: str
    ) -> None:
        with self.accounting_store.transaction() as connection:
            row = connection.execute(
                """
                SELECT is_algo, exchange_order_id FROM owned_exchange_orders
                WHERE client_order_id = ? AND algo_id IS NOT NULL
                """,
                (client_order_id,),
            ).fetchone()
            if row is None or int(row["is_algo"]) != 1:
                raise DuplicateEventConflict(
                    "algo trigger confirmation has no prepared owner"
                )
            current = row["exchange_order_id"]
            if current is not None and str(current) != exchange_order_id:
                raise DuplicateEventConflict(
                    "algo order resolved to conflicting actual orders"
                )
            connection.execute(
                """
                UPDATE owned_exchange_orders SET exchange_order_id = ?
                WHERE client_order_id = ?
                """,
                (exchange_order_id, client_order_id),
            )

    def intent_for_order(self, exchange_order_id: str) -> sqlite3.Row | None:
        with self.accounting_store.read_connection() as connection:
            row = connection.execute(
                """
                SELECT i.*, o.role, o.client_order_id, o.is_algo,
                       o.exchange_order_id
                FROM owned_exchange_orders o
                JOIN execution_intents i ON i.intent_id = o.intent_id
                WHERE o.exchange_order_id = ?
                """,
                (exchange_order_id,),
            ).fetchone()
        return cast(sqlite3.Row | None, row)

    def intent_for_funding(
        self, *, symbol: str, occurred_at: datetime
    ) -> sqlite3.Row | None:
        with self.accounting_store.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_intents
                WHERE symbol = ? AND opened_at <= ?
                  AND (closed_at IS NULL OR closed_at >= ?)
                """,
                (symbol, occurred_at.isoformat(), occurred_at.isoformat()),
            ).fetchall()
        if len(rows) != 1:
            return None
        return cast(sqlite3.Row, rows[0])

    def list_symbols(self, *, start: datetime, end: datetime) -> tuple[str, ...]:
        with self.accounting_store.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT symbol FROM execution_intents
                WHERE opened_at <= ? AND (closed_at IS NULL OR closed_at >= ?)
                ORDER BY symbol
                """,
                (end.isoformat(), start.isoformat()),
            ).fetchall()
        return tuple(str(row["symbol"]) for row in rows)

    def unresolved_orders(self) -> tuple[sqlite3.Row, ...]:
        with self.accounting_store.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT o.*, i.symbol FROM owned_exchange_orders o
                JOIN execution_intents i ON i.intent_id = o.intent_id
                WHERE o.exchange_order_id IS NULL
                ORDER BY o.created_at
                """
            ).fetchall()
        return tuple(rows)

    def record_shadow_event(
        self,
        *,
        event_id: str,
        event_type: str,
        occurred_at: datetime,
        payload: Mapping[str, Any],
        status: str,
        reason: str,
        observed_at: datetime,
    ) -> bool:
        payload_json = _canonical_json(payload)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        with self.accounting_store.transaction() as connection:
            row = connection.execute(
                "SELECT payload_hash FROM shadow_exchange_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is not None:
                if str(row["payload_hash"]) != payload_hash:
                    raise DuplicateEventConflict(
                        "exchange event ID was reused with different payload"
                    )
                return False
            connection.execute(
                """
                INSERT INTO shadow_exchange_events(
                    event_id, event_type, occurred_at, payload_json, payload_hash,
                    status, reason, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_type,
                    occurred_at.isoformat(),
                    payload_json,
                    payload_hash,
                    status,
                    reason,
                    observed_at.isoformat(),
                ),
            )
        return True

    def mark_posted(self, event_id: str) -> None:
        with self.accounting_store.transaction() as connection:
            connection.execute(
                """
                UPDATE shadow_exchange_events
                SET status = 'POSTED', reason = 'posted to investor ledger'
                WHERE event_id = ? AND status = 'VERIFIED_PENDING_LEDGER'
                """,
                (event_id,),
            )

    def event_status(self, event_id: str) -> str | None:
        with self.accounting_store.read_connection() as connection:
            row = connection.execute(
                "SELECT status FROM shadow_exchange_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return None if row is None else str(row["status"])

    def settlement_totals(self, intent_id: str) -> OwnedSettlementTotals:
        """Sum only verified exchange events durably attributed to one intent."""

        with self.accounting_store.read_connection() as connection:
            intent = connection.execute(
                "SELECT 1 FROM execution_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if intent is None:
                raise ValueError("cannot settle an unknown execution intent")
            rows = connection.execute(
                """
                SELECT event_type, payload_json
                FROM shadow_exchange_events
                WHERE status IN ('VERIFIED_PENDING_LEDGER', 'POSTED')
                ORDER BY occurred_at, event_id
                """
            ).fetchall()

        realized_pnl = Decimal("0")
        commission = Decimal("0")
        funding = Decimal("0")
        event_count = 0
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            if payload.get("intent_id") != intent_id:
                continue
            if row["event_type"] == "TRADE":
                realized_pnl += Decimal(str(payload["realized_pnl"]))
                commission += Decimal(str(payload["commission"]))
            elif row["event_type"] == "FUNDING":
                funding += Decimal(str(payload["amount"]))
            else:  # pragma: no cover - protected by the database constraint
                raise ValueError("unsupported shadow event type")
            event_count += 1

        return OwnedSettlementTotals(
            intent_id=intent_id,
            realized_pnl=realized_pnl,
            commission=commission,
            funding=funding,
            event_count=event_count,
        )
