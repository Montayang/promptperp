from __future__ import annotations

from datetime import datetime
from typing import Protocol

from promptperp.execution.models import TradeIntent


class ExecutionOwnershipSink(Protocol):
    """Durably record identities needed to prove exchange-event ownership."""

    def begin_intent(self, intent: TradeIntent, *, opened_at: datetime) -> None: ...

    def prepare_order(
        self,
        *,
        intent_id: str,
        role: str,
        client_order_id: str,
        is_algo: bool,
        created_at: datetime,
    ) -> None: ...

    def confirm_order(
        self,
        *,
        client_order_id: str,
        exchange_order_id: str,
    ) -> None: ...

    def confirm_algo_trigger(
        self,
        *,
        client_order_id: str,
        exchange_order_id: str,
    ) -> None: ...

    def close_intent(self, intent_id: str, *, closed_at: datetime) -> None: ...
