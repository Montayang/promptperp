from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from promptperp.accounting.models import require_utc
from promptperp.notifications.email_outbox import EmailSender
from promptperp.storage.reporting_sqlite import SQLiteReportingStore


class DeliveryUncertain(RuntimeError):
    """SMTP may have accepted a message; automatic retry is unsafe."""


@dataclass(frozen=True)
class DeliveryResult:
    message_id: str
    delivery_id: str
    status: str = "SENT"


class OutboxDeliveryService:
    def __init__(self, store: SQLiteReportingStore, sender: EmailSender):
        self._store = store
        self._sender = sender

    def deliver(
        self,
        *,
        message_id: str,
        delivery_id: str,
        occurred_at: datetime,
    ) -> DeliveryResult:
        require_utc(occurred_at, "delivery occurred_at")
        message = self._store.claim_message(
            message_id,
            delivery_id=delivery_id,
            occurred_at=occurred_at,
        )
        try:
            self._sender.send(message)
            self._store.mark_sent(
                message_id,
                delivery_id=delivery_id,
                occurred_at=occurred_at,
            )
        except Exception as exc:
            raise DeliveryUncertain(
                f"delivery outcome is uncertain for {message_id}; do not retry automatically"
            ) from exc
        return DeliveryResult(message_id=message_id, delivery_id=delivery_id)
