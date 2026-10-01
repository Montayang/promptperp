from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol

from promptperp.accounting.exchange_events import (
    ExchangeFunding,
    ExchangeTrade,
    ShadowSyncReport,
)
from promptperp.accounting.models import TradingSettlement
from promptperp.accounting.service import InvestorAccountingService
from promptperp.storage.shadow_events_sqlite import SQLiteShadowEventStore


class AccountEventReader(Protocol):
    def list_trades(
        self, *, symbol: str, start_ms: int, end_ms: int
    ) -> tuple[ExchangeTrade, ...]: ...

    def list_funding(
        self, *, start_ms: int, end_ms: int
    ) -> tuple[ExchangeFunding, ...]: ...

    def resolve_order(self, *, symbol: str, client_order_id: str) -> str | None: ...

    def resolve_algo_order(
        self,
        *,
        symbol: str,
        client_order_id: str,
        algo_id: str | None,
    ) -> tuple[str | None, str | None]: ...


class ShadowAccountingSync:
    """Verify private account events against durable execution ownership."""

    def __init__(
        self,
        *,
        reader: AccountEventReader,
        shadow_store: SQLiteShadowEventStore,
        accounting_service: InvestorAccountingService | None = None,
        post_to_ledger: bool = False,
    ):
        if post_to_ledger and accounting_service is None:
            raise ValueError("ledger posting requires the accounting service")
        self.reader = reader
        self.shadow_store = shadow_store
        self.accounting_service = accounting_service
        self.post_to_ledger = post_to_ledger

    def sync(self, *, start: datetime, end: datetime) -> ShadowSyncReport:
        self._validate_window(start, end)
        started_at = datetime.now(timezone.utc)
        verified = posted = duplicates = quarantined = 0

        for order in self.shadow_store.unresolved_orders():
            client_id = str(order["client_order_id"])
            if int(order["is_algo"]):
                algo_id, actual_order_id = self.reader.resolve_algo_order(
                    symbol=str(order["symbol"]),
                    client_order_id=client_id,
                    algo_id=(
                        None if order["algo_id"] is None else str(order["algo_id"])
                    ),
                )
                if algo_id is not None and order["algo_id"] is None:
                    self.shadow_store.confirm_order(
                        client_order_id=client_id,
                        exchange_order_id=algo_id,
                    )
                if actual_order_id is not None:
                    self.shadow_store.confirm_algo_trigger(
                        client_order_id=client_id,
                        exchange_order_id=actual_order_id,
                    )
            else:
                actual_order_id = self.reader.resolve_order(
                    symbol=str(order["symbol"]), client_order_id=client_id
                )
                if actual_order_id is not None:
                    self.shadow_store.confirm_order(
                        client_order_id=client_id,
                        exchange_order_id=actual_order_id,
                    )

        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        for symbol in self.shadow_store.list_symbols(start=start, end=end):
            for trade in self.reader.list_trades(
                symbol=symbol, start_ms=start_ms, end_ms=end_ms
            ):
                state = self._ingest_trade(trade, observed_at=started_at)
                verified += state == "verified"
                posted += state == "posted"
                duplicates += state == "duplicate"
                quarantined += state == "quarantined"

        for funding in self.reader.list_funding(start_ms=start_ms, end_ms=end_ms):
            state = self._ingest_funding(funding, observed_at=started_at)
            verified += state == "verified"
            posted += state == "posted"
            duplicates += state == "duplicate"
            quarantined += state == "quarantined"

        return ShadowSyncReport(
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            verified_events=verified,
            posted_events=posted,
            duplicate_events=duplicates,
            quarantined_events=quarantined,
        )

    def _ingest_trade(self, trade: ExchangeTrade, *, observed_at: datetime) -> str:
        owner = self.shadow_store.intent_for_order(trade.order_id)
        payload = {
            "symbol": trade.symbol,
            "trade_id": trade.trade_id,
            "order_id": trade.order_id,
            "realized_pnl": str(trade.realized_pnl),
            "commission": str(trade.commission),
            "commission_asset": trade.commission_asset,
        }
        if owner is None or str(owner["symbol"]) != trade.symbol:
            return self._record(
                event_id=trade.event_id,
                event_type="TRADE",
                occurred_at=trade.occurred_at,
                payload=payload,
                status="QUARANTINED",
                reason="trade order has no unique owned execution",
                observed_at=observed_at,
            )
        payload.update(self._ownership_payload(owner))
        settlement = TradingSettlement(
            event_id=trade.event_id,
            occurred_at=trade.occurred_at,
            strategy_id=str(owner["strategy_id"]),
            run_id=str(owner["run_id"]),
            intent_id=str(owner["intent_id"]),
            order_id=trade.order_id,
            trade_id=trade.trade_id,
            realized_pnl=trade.realized_pnl,
            commission=trade.commission,
            funding=Decimal("0"),
        )
        return self._record_verified(settlement, "TRADE", payload, observed_at)

    def _ingest_funding(
        self, funding: ExchangeFunding, *, observed_at: datetime
    ) -> str:
        owner = self.shadow_store.intent_for_funding(
            symbol=funding.symbol, occurred_at=funding.occurred_at
        )
        payload = {
            "symbol": funding.symbol,
            "transaction_id": funding.transaction_id,
            "amount": str(funding.amount),
            "asset": funding.asset,
        }
        if owner is None:
            return self._record(
                event_id=funding.event_id,
                event_type="FUNDING",
                occurred_at=funding.occurred_at,
                payload=payload,
                status="QUARANTINED",
                reason="funding has no unique owned position interval",
                observed_at=observed_at,
            )
        payload.update(self._ownership_payload(owner))
        settlement = TradingSettlement(
            event_id=funding.event_id,
            occurred_at=funding.occurred_at,
            strategy_id=str(owner["strategy_id"]),
            run_id=str(owner["run_id"]),
            intent_id=str(owner["intent_id"]),
            order_id=f"funding:{funding.transaction_id}",
            trade_id=funding.transaction_id,
            realized_pnl=Decimal("0"),
            commission=Decimal("0"),
            funding=funding.amount,
        )
        return self._record_verified(settlement, "FUNDING", payload, observed_at)

    def _record_verified(
        self,
        settlement: TradingSettlement,
        event_type: str,
        payload: dict[str, str],
        observed_at: datetime,
    ) -> str:
        current = self.shadow_store.event_status(settlement.event_id)
        if current == "POSTED" or (current is not None and not self.post_to_ledger):
            return "duplicate"
        if current is None:
            created = self.shadow_store.record_shadow_event(
                event_id=settlement.event_id,
                event_type=event_type,
                occurred_at=settlement.occurred_at,
                payload=payload,
                status="VERIFIED_PENDING_LEDGER",
                reason="ownership verified; investor ledger posting disabled",
                observed_at=observed_at,
            )
            if not created:
                return "duplicate"
        if not self.post_to_ledger:
            return "verified"
        assert self.accounting_service is not None
        if any(
            value != 0
            for value in (
                settlement.realized_pnl,
                settlement.commission,
                settlement.funding,
            )
        ):
            self.accounting_service.record_settlement(settlement)
        self.shadow_store.mark_posted(settlement.event_id)
        return "posted"

    def _record(
        self,
        *,
        event_id: str,
        event_type: str,
        occurred_at: datetime,
        payload: dict[str, str],
        status: str,
        reason: str,
        observed_at: datetime,
    ) -> str:
        created = self.shadow_store.record_shadow_event(
            event_id=event_id,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=payload,
            status=status,
            reason=reason,
            observed_at=observed_at,
        )
        return "quarantined" if created else "duplicate"

    @staticmethod
    def _validate_window(start: datetime, end: datetime) -> None:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("shadow sync timestamps must be timezone-aware")
        if start >= end:
            raise ValueError("shadow sync window must be increasing")
        if end - start > timedelta(days=7):
            raise ValueError("shadow sync window cannot exceed seven days")

    @staticmethod
    def _ownership_payload(owner: Any) -> dict[str, str]:
        return {
            "strategy_id": str(owner["strategy_id"]),
            "run_id": str(owner["run_id"]),
            "intent_id": str(owner["intent_id"]),
        }
