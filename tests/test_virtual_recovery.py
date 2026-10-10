from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from promptperp.domain import (
    Fill,
    FuturesTradeFill,
    OrderSide,
    PositionSide,
    ReconciliationFailed,
)
from promptperp.execution.virtual_fills import VirtualFillAttributor
from promptperp.execution.virtual_positions import (
    VirtualPositionError,
    VirtualPositionStore,
)
from promptperp.execution.virtual_recovery import VirtualFillRecovery
from promptperp.execution.virtual_routes import (
    VirtualFillRole,
    VirtualFillRouteRegistry,
)

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


class Reader:
    def __init__(self, fills):
        self.fills = tuple(fills)
        self.calls = []

    def list_fills(self, *, symbol, start_ms, end_ms):
        self.calls.append((symbol, start_ms, end_ms))
        return tuple(fill for fill in self.fills if fill.fill.symbol == symbol)


def _trade(*, order, trade, symbol="BTCUSDT", quantity="1"):
    return FuturesTradeFill(
        fill=Fill(
            symbol=symbol,
            order_id=order,
            trade_id=trade,
            side=OrderSide.BUY,
            position_side=PositionSide.LONG,
            quantity=Decimal(quantity),
            price=Decimal("100"),
            commission=Decimal("0.1"),
            commission_asset="USDT",
        ),
        occurred_at=NOW,
        realized_pnl=Decimal("0"),
    )


def _prepare(registry, *, client, exchange, strategy, symbol="BTCUSDT"):
    registry.prepare(
        strategy_id=strategy,
        run_id=f"{strategy}-run",
        symbol=symbol,
        position_side=PositionSide.LONG,
        client_order_id=client,
        role=VirtualFillRole.OPEN,
    )
    registry.confirm(client_order_id=client, exchange_order_id=exchange)


def _recovery(tmp_path, fills):
    positions = VirtualPositionStore(tmp_path / "positions.sqlite3")
    positions.initialize()
    routes = VirtualFillRouteRegistry(tmp_path / "routes.sqlite3")
    routes.initialize()
    reader = Reader(fills)
    recovery = VirtualFillRecovery(
        reader=reader,
        route_lookup=routes,
        attributor=VirtualFillAttributor(positions),
    )
    return recovery, routes, positions


def test_same_symbol_fills_recover_to_their_distinct_strategy_owners(tmp_path) -> None:
    recovery, routes, positions = _recovery(
        tmp_path,
        (_trade(order="order-a", trade="1"), _trade(order="order-b", trade="2")),
    )
    _prepare(routes, client="client-a", exchange="order-a", strategy="strategy_alpha")
    _prepare(routes, client="client-b", exchange="order-b", strategy="strategy_beta")

    report = recovery.recover(
        symbols=("BTCUSDT",), start=NOW - timedelta(minutes=1), end=NOW
    )

    assert report.recovered_fills == 2
    assert positions.position(
        "strategy_alpha", "strategy_alpha-run", "BTCUSDT", PositionSide.LONG
    ).quantity == Decimal("1")
    assert positions.position(
        "strategy_beta", "strategy_beta-run", "BTCUSDT", PositionSide.LONG
    ).quantity == Decimal("1")


def test_unknown_order_rejects_entire_batch_before_any_position_write(tmp_path) -> None:
    recovery, routes, positions = _recovery(
        tmp_path,
        (_trade(order="owned", trade="1"), _trade(order="unknown", trade="2")),
    )
    _prepare(routes, client="client-owned", exchange="owned", strategy="strategy_beta")

    with pytest.raises(ReconciliationFailed, match="without virtual ownership"):
        recovery.recover(
            symbols=("BTCUSDT",), start=NOW - timedelta(minutes=1), end=NOW
        )

    with pytest.raises(VirtualPositionError, match="does not exist"):
        positions.position(
            "strategy_beta", "strategy_beta-run", "BTCUSDT", PositionSide.LONG
        )


def test_recovery_replay_is_idempotent(tmp_path) -> None:
    recovery, routes, positions = _recovery(
        tmp_path, (_trade(order="owned", trade="1"),)
    )
    _prepare(routes, client="client-owned", exchange="owned", strategy="strategy_beta")

    for _ in range(2):
        recovery.recover(
            symbols=("BTCUSDT",), start=NOW - timedelta(minutes=1), end=NOW
        )

    assert positions.position(
        "strategy_beta", "strategy_beta-run", "BTCUSDT", PositionSide.LONG
    ).quantity == Decimal("1")


def test_order_ids_are_scoped_by_symbol_during_recovery(tmp_path):
    recovery, routes, positions = _recovery(
        tmp_path,
        (
            _trade(order="same-id", trade="1", symbol="BTCUSDT"),
            _trade(order="same-id", trade="1", symbol="ETHUSDT"),
        ),
    )
    _prepare(routes, client="a", exchange="same-id", strategy="alpha", symbol="BTCUSDT")
    _prepare(routes, client="b", exchange="same-id", strategy="beta", symbol="ETHUSDT")
    result = recovery.recover(
        symbols=("BTCUSDT", "ETHUSDT"), start=NOW - timedelta(minutes=1), end=NOW
    )
    assert result.recovered_fills == 2
    assert (
        positions.position("alpha", "alpha-run", "BTCUSDT", PositionSide.LONG).quantity
        == 1
    )
    assert (
        positions.position("beta", "beta-run", "ETHUSDT", PositionSide.LONG).quantity
        == 1
    )
