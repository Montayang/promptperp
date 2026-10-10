from __future__ import annotations

import pytest

from promptperp.domain import PositionSide
from promptperp.execution.virtual_routes import (
    VirtualFillRole,
    VirtualFillRouteError,
    VirtualFillRouteRegistry,
)


def _registry(tmp_path):
    registry = VirtualFillRouteRegistry(tmp_path / "routes.sqlite3")
    registry.initialize()
    return registry


def _prepare(registry, *, client="client-1", strategy="strategy_beta"):
    registry.prepare(
        strategy_id=strategy,
        run_id="run-1",
        symbol="BTCUSDT",
        position_side=PositionSide.LONG,
        client_order_id=client,
        role=VirtualFillRole.OPEN,
    )


def test_route_survives_restart_and_resolves_one_exchange_order(tmp_path) -> None:
    registry = _registry(tmp_path)
    _prepare(registry)
    registry.confirm(client_order_id="client-1", exchange_order_id="exchange-1")

    recovered = _registry(tmp_path).route_for_order("exchange-1")

    assert recovered is not None
    assert recovered.strategy_id == "strategy_beta"
    assert recovered.run_id == "run-1"
    assert recovered.position_side is PositionSide.LONG
    assert recovered.role is VirtualFillRole.OPEN


def test_prepared_route_resolves_owner_before_exchange_confirmation(tmp_path) -> None:
    registry = _registry(tmp_path)
    _prepare(registry)

    owner = _registry(tmp_path).owner_for_client("client-1")

    assert owner is not None
    assert owner.strategy_id == "strategy_beta"
    assert owner.run_id == "run-1"
    assert owner.symbol == "BTCUSDT"
    assert owner.position_side is PositionSide.LONG
    assert owner.role is VirtualFillRole.OPEN


def test_route_registration_is_idempotent_but_conflicts_fail_closed(tmp_path) -> None:
    registry = _registry(tmp_path)
    _prepare(registry)
    _prepare(registry)

    with pytest.raises(VirtualFillRouteError, match="conflicting virtual ownership"):
        _prepare(registry, strategy="strategy_alpha")

    registry.confirm(client_order_id="client-1", exchange_order_id="exchange-1")
    registry.confirm(client_order_id="client-1", exchange_order_id="exchange-1")
    with pytest.raises(VirtualFillRouteError, match="conflicting exchange orders"):
        registry.confirm(client_order_id="client-1", exchange_order_id="exchange-2")


def test_exchange_order_cannot_be_claimed_by_two_clients(tmp_path) -> None:
    registry = _registry(tmp_path)
    _prepare(registry, client="client-1")
    _prepare(registry, client="client-2")
    registry.confirm(client_order_id="client-1", exchange_order_id="exchange-1")

    with pytest.raises(VirtualFillRouteError, match="another virtual owner"):
        registry.confirm(client_order_id="client-2", exchange_order_id="exchange-1")


def test_exchange_order_ids_are_scoped_by_symbol(tmp_path) -> None:
    registry = _registry(tmp_path)
    _prepare(registry)
    registry.confirm(client_order_id="client-1", exchange_order_id="101")
    registry.prepare(
        strategy_id="strategy_beta",
        run_id="run-1",
        symbol="ETHUSDT",
        position_side=PositionSide.SHORT,
        client_order_id="client-2",
        role=VirtualFillRole.OPEN,
    )
    registry.confirm(client_order_id="client-2", exchange_order_id="101")
    assert (
        registry.route_for_order("101", symbol="BTCUSDT").position_side
        is PositionSide.LONG
    )
    assert (
        registry.route_for_order("101", symbol="ETHUSDT").position_side
        is PositionSide.SHORT
    )
    with pytest.raises(VirtualFillRouteError, match="symbol scope"):
        registry.route_for_order("101")
