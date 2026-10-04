from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from promptperp.domain import (
    Fill,
    FuturesTradeFill,
    OrderSide,
    PositionSide,
    ReconciliationFailed,
)
from promptperp.execution.virtual_fills import (
    VirtualFillAttributor,
    VirtualFillRole,
    VirtualFillRoute,
)
from promptperp.execution.virtual_positions import VirtualPositionStore

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def _fill(*, order, trade, side, quantity, price="100", commission="0.1"):
    return Fill(
        symbol="BTCUSDT",
        order_id=order,
        trade_id=trade,
        side=side,
        position_side=PositionSide.LONG,
        quantity=Decimal(quantity),
        price=Decimal(price),
        commission=Decimal(commission),
        commission_asset="USDT",
    )


def _route(strategy, run, order, role):
    return VirtualFillRoute(
        strategy_id=strategy,
        run_id=run,
        symbol="BTCUSDT",
        position_side=PositionSide.LONG,
        exchange_order_id=order,
        role=role,
    )


def test_same_symbol_fills_remain_owned_and_actual_pnl_is_attributed(tmp_path) -> None:
    store = VirtualPositionStore(tmp_path / "positions.sqlite3")
    store.initialize()
    attributor = VirtualFillAttributor(store)
    attributor.apply(
        fill=_fill(
            order="strategy_alpha-open", trade="1", side=OrderSide.BUY, quantity="2"
        ),
        route=_route(
            "strategy_alpha",
            "strategy_alpha-run",
            "strategy_alpha-open",
            VirtualFillRole.OPEN,
        ),
        realized_pnl=Decimal("0"),
        occurred_at=NOW,
    )
    attributor.apply(
        fill=_fill(
            order="strategy_beta-open", trade="2", side=OrderSide.BUY, quantity="3"
        ),
        route=_route(
            "strategy_beta",
            "strategy_beta-run",
            "strategy_beta-open",
            VirtualFillRole.OPEN,
        ),
        realized_pnl=Decimal("0"),
        occurred_at=NOW,
    )
    closed = attributor.apply(
        fill=_fill(
            order="strategy_alpha-close",
            trade="3",
            side=OrderSide.SELL,
            quantity="2",
            price="110",
        ),
        route=_route(
            "strategy_alpha",
            "strategy_alpha-run",
            "strategy_alpha-close",
            VirtualFillRole.REDUCE,
        ),
        realized_pnl=Decimal("18.75"),
        occurred_at=NOW,
    )

    assert closed.quantity == 0
    assert closed.realized_pnl == Decimal("18.75")
    assert closed.commission == Decimal("0.2")
    assert (
        store.position(
            "strategy_beta", "strategy_beta-run", "BTCUSDT", PositionSide.LONG
        ).quantity
        == 3
    )


def test_fill_replay_is_idempotent_and_mismatch_fails_closed(tmp_path) -> None:
    store = VirtualPositionStore(tmp_path / "positions.sqlite3")
    store.initialize()
    attributor = VirtualFillAttributor(store)
    fill = _fill(order="open-1", trade="1", side=OrderSide.BUY, quantity="1")
    route = _route("strategy_beta", "run", "open-1", VirtualFillRole.OPEN)
    first = attributor.apply(
        fill=fill, route=route, realized_pnl=Decimal("0"), occurred_at=NOW
    )
    replay = attributor.apply(
        fill=fill, route=route, realized_pnl=Decimal("0"), occurred_at=NOW
    )
    assert replay == first

    with pytest.raises(ReconciliationFailed, match="does not match"):
        attributor.apply(
            fill=fill,
            route=_route("strategy_beta", "run", "another-order", VirtualFillRole.OPEN),
            realized_pnl=Decimal("0"),
            occurred_at=NOW,
        )


def test_opening_fill_with_realized_pnl_is_rejected(tmp_path) -> None:
    store = VirtualPositionStore(tmp_path / "positions.sqlite3")
    store.initialize()
    attributor = VirtualFillAttributor(store)
    with pytest.raises(ReconciliationFailed, match="unexpectedly realized"):
        attributor.apply(
            fill=_fill(order="open-1", trade="1", side=OrderSide.BUY, quantity="1"),
            route=_route("strategy_beta", "run", "open-1", VirtualFillRole.OPEN),
            realized_pnl=Decimal("1"),
            occurred_at=NOW,
        )


def test_normalized_trade_is_applied_to_its_single_virtual_owner(tmp_path) -> None:
    store = VirtualPositionStore(tmp_path / "positions.sqlite3")
    store.initialize()
    attributor = VirtualFillAttributor(store)
    trade = FuturesTradeFill(
        fill=_fill(
            order="strategy_beta-open",
            trade="trade-1",
            side=OrderSide.BUY,
            quantity="1.5",
        ),
        occurred_at=NOW,
        realized_pnl=Decimal("0"),
    )

    position = attributor.apply_trade(
        trade=trade,
        route=_route(
            "strategy_beta", "run", "strategy_beta-open", VirtualFillRole.OPEN
        ),
    )

    assert position.quantity == Decimal("1.5")
    assert position.strategy_id == "strategy_beta"
    assert position.run_id == "run"
