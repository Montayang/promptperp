from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from promptperp.domain import FuturesTradeFill, ReconciliationFailed
from promptperp.execution.virtual_fills import VirtualFillAttributor
from promptperp.execution.virtual_routes import VirtualFillRoute


class FuturesFillReader(Protocol):
    def list_fills(
        self, *, symbol: str, start_ms: int, end_ms: int
    ) -> tuple[FuturesTradeFill, ...]: ...


class VirtualFillRouteLookup(Protocol):
    def route_for_order(
        self, exchange_order_id: str, *, symbol: str | None = None
    ) -> VirtualFillRoute | None: ...


@dataclass(frozen=True)
class VirtualFillRecoveryReport:
    symbols: tuple[str, ...]
    recovered_fills: int
    started_at: datetime
    completed_at: datetime


class VirtualFillRecovery:
    """Recover owned fills only after the complete batch proves its routes."""

    def __init__(
        self,
        *,
        reader: FuturesFillReader,
        route_lookup: VirtualFillRouteLookup,
        attributor: VirtualFillAttributor,
    ):
        self.reader = reader
        self.route_lookup = route_lookup
        self.attributor = attributor

    def recover(
        self, *, symbols: tuple[str, ...], start: datetime, end: datetime
    ) -> VirtualFillRecoveryReport:
        normalized = tuple(sorted(set(symbols)))
        if not normalized or any(
            not symbol or symbol != symbol.upper() for symbol in normalized
        ):
            raise ValueError("recovery symbols must be unique uppercase identities")
        if start.tzinfo is None or end.tzinfo is None or end < start:
            raise ValueError("recovery window must use aware ordered times")
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        fills = tuple(
            sorted(
                (
                    fill
                    for symbol in normalized
                    for fill in self.reader.list_fills(
                        symbol=symbol,
                        start_ms=start_ms,
                        end_ms=end_ms,
                    )
                ),
                key=lambda item: (item.occurred_at, item.event_id),
            )
        )
        event_ids = [fill.event_id for fill in fills]
        if len(set(event_ids)) != len(event_ids):
            raise ReconciliationFailed(
                "fill recovery returned duplicate event identities"
            )

        routed: list[tuple[FuturesTradeFill, VirtualFillRoute]] = []
        for trade in fills:
            if (
                trade.fill.symbol not in normalized
                or not start <= trade.occurred_at <= end
            ):
                raise ReconciliationFailed(
                    "fill recovery returned data outside its scope"
                )
            route = self.route_lookup.route_for_order(
                trade.fill.order_id, symbol=trade.fill.symbol
            )
            if route is None:
                raise ReconciliationFailed(
                    "fill recovery found an order without virtual ownership"
                )
            self.attributor.validate(
                fill=trade.fill,
                route=route,
                realized_pnl=trade.realized_pnl,
                occurred_at=trade.occurred_at,
            )
            routed.append((trade, route))

        for trade, route in routed:
            self.attributor.apply_trade(trade=trade, route=route)
        return VirtualFillRecoveryReport(
            symbols=normalized,
            recovered_fills=len(routed),
            started_at=start,
            completed_at=end,
        )
