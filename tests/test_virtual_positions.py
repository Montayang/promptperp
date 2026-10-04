from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from promptperp.domain import FuturesPosition, PositionSide
from promptperp.execution.virtual_positions import (
    ExchangePositionMode,
    VirtualPositionError,
    VirtualPositionStore,
)

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def test_funding_replay_keeps_original_allocation_after_positions_close(tmp_path):
    store = VirtualPositionStore(tmp_path / "positions.sqlite3")
    store.initialize()
    for owner in ("a", "b"):
        _open(
            store,
            event=owner,
            strategy=owner,
            run="run",
            side=PositionSide.LONG,
            quantity="1.000",
            price="100",
        )
    kwargs = dict(
        event_id="funding",
        symbol="BTCUSDT",
        position_side=PositionSide.LONG,
        total_funding=Decimal("-1"),
        mark_price=Decimal("100"),
        occurred_at=NOW,
    )
    original = store.allocate_funding(**kwargs)
    for owner in ("a", "b"):
        store.close_fill(
            event_id="close-" + owner,
            strategy_id=owner,
            run_id="run",
            symbol="BTCUSDT",
            position_side=PositionSide.LONG,
            quantity=Decimal("1.000"),
            price=Decimal("100"),
            commission=Decimal("0"),
            occurred_at=NOW,
        )
    assert not store.positions()
    assert VirtualPositionStore(store.path).allocate_funding(**kwargs) == original
    assert store.settlement("a", "run").funding == Decimal("-0.5")


def test_new_funding_does_not_allocate_residual_to_decimal_zero_lot(tmp_path):
    store = VirtualPositionStore(tmp_path / "positions.sqlite3")
    store.initialize()
    for owner in ("a", "z"):
        _open(
            store,
            event=owner,
            strategy=owner,
            run="run",
            side=PositionSide.LONG,
            quantity="1.000",
            price="100",
        )
    store.close_fill(
        event_id="close-z",
        strategy_id="z",
        run_id="run",
        symbol="BTCUSDT",
        position_side=PositionSide.LONG,
        quantity=Decimal("1.000"),
        price=Decimal("100"),
        commission=Decimal("0"),
        occurred_at=NOW,
    )
    result = store.allocate_funding(
        event_id="funding",
        symbol="BTCUSDT",
        position_side=PositionSide.LONG,
        total_funding=Decimal("-0.123456789"),
        mark_price=Decimal("100"),
        occurred_at=NOW,
    )
    assert result == {("a", "run"): Decimal("-0.123456789")}


def _open(
    store: VirtualPositionStore,
    *,
    event: str,
    strategy: str,
    run: str,
    side: PositionSide,
    quantity: str,
    price: str,
):
    return store.open_fill(
        event_id=event,
        strategy_id=strategy,
        run_id=run,
        symbol="BTCUSDT",
        position_side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        commission=Decimal("0.01"),
        occurred_at=NOW,
    )


def test_two_strategies_can_own_and_close_same_symbol_same_side(tmp_path) -> None:
    store = VirtualPositionStore(tmp_path / "positions.sqlite3")
    store.initialize()
    _open(
        store,
        event="strategy_alpha-open",
        strategy="strategy_alpha",
        run="strategy_alpha-run",
        side=PositionSide.LONG,
        quantity="2",
        price="100",
    )
    _open(
        store,
        event="strategy_beta-open",
        strategy="strategy_beta",
        run="strategy_beta-run",
        side=PositionSide.LONG,
        quantity="3",
        price="120",
    )

    assert (
        store.reconcile(
            (
                FuturesPosition(
                    "BTCUSDT", PositionSide.LONG, Decimal("5"), Decimal("112")
                ),
            ),
            position_mode=ExchangePositionMode.HEDGE,
        )
        == ()
    )

    strategy_alpha = store.close_fill(
        event_id="strategy_alpha-close",
        strategy_id="strategy_alpha",
        run_id="strategy_alpha-run",
        symbol="BTCUSDT",
        position_side=PositionSide.LONG,
        quantity=Decimal("2"),
        price=Decimal("130"),
        commission=Decimal("0.01"),
        occurred_at=NOW,
    )
    strategy_beta = store.position(
        "strategy_beta", "strategy_beta-run", "BTCUSDT", PositionSide.LONG
    )
    assert strategy_alpha.quantity == 0
    assert strategy_alpha.realized_pnl == Decimal("60")
    assert strategy_beta.quantity == 3
    assert strategy_beta.average_entry_price == Decimal("120")
    assert (
        store.reconcile(
            (
                FuturesPosition(
                    "BTCUSDT", PositionSide.LONG, Decimal("3"), Decimal("112")
                ),
            ),
            position_mode=ExchangePositionMode.HEDGE,
        )
        == ()
    )


def test_opposite_directions_and_funding_remain_separately_attributed(tmp_path) -> None:
    store = VirtualPositionStore(tmp_path / "positions.sqlite3")
    store.initialize()
    _open(
        store,
        event="long-a",
        strategy="a",
        run="a-run",
        side=PositionSide.LONG,
        quantity="1",
        price="100",
    )
    _open(
        store,
        event="long-b",
        strategy="b",
        run="b-run",
        side=PositionSide.LONG,
        quantity="3",
        price="100",
    )
    _open(
        store,
        event="short-c",
        strategy="c",
        run="c-run",
        side=PositionSide.SHORT,
        quantity="2",
        price="100",
    )
    allocated = store.allocate_funding(
        event_id="funding-long",
        symbol="BTCUSDT",
        position_side=PositionSide.LONG,
        total_funding=Decimal("-0.10000001"),
        mark_price=Decimal("101"),
        occurred_at=NOW,
    )
    assert sum(allocated.values()) == Decimal("-0.10000001")
    assert allocated[("a", "a-run")] == Decimal("-0.02500000")
    assert allocated[("b", "b-run")] == Decimal("-0.07500001")
    assert store.position("c", "c-run", "BTCUSDT", PositionSide.SHORT).funding == 0


def test_one_way_projection_reconciles_merged_opposite_strategy_positions(
    tmp_path,
) -> None:
    store = VirtualPositionStore(tmp_path / "positions.sqlite3")
    store.initialize()
    _open(
        store,
        event="long-a",
        strategy="a",
        run="a-run",
        side=PositionSide.LONG,
        quantity="5",
        price="100",
    )
    _open(
        store,
        event="short-b",
        strategy="b",
        run="b-run",
        side=PositionSide.SHORT,
        quantity="2",
        price="101",
    )

    assert store.physical_quantities(position_mode=ExchangePositionMode.ONE_WAY) == {
        ("BTCUSDT", PositionSide.LONG): Decimal("3")
    }
    assert (
        store.reconcile(
            (
                FuturesPosition(
                    "BTCUSDT", PositionSide.LONG, Decimal("3"), Decimal("100")
                ),
            ),
            position_mode=ExchangePositionMode.ONE_WAY,
        )
        == ()
    )
    assert store.position("a", "a-run", "BTCUSDT", PositionSide.LONG).quantity == 5
    assert store.position("b", "b-run", "BTCUSDT", PositionSide.SHORT).quantity == 2


def test_account_position_mode_changes_the_required_physical_shape(tmp_path) -> None:
    store = VirtualPositionStore(tmp_path / "positions.sqlite3")
    store.initialize()
    _open(
        store,
        event="long-a",
        strategy="a",
        run="a-run",
        side=PositionSide.LONG,
        quantity="2",
        price="100",
    )
    _open(
        store,
        event="short-b",
        strategy="b",
        run="b-run",
        side=PositionSide.SHORT,
        quantity="2",
        price="100",
    )

    observed_flat: tuple[FuturesPosition, ...] = ()
    assert (
        store.reconcile(observed_flat, position_mode=ExchangePositionMode.ONE_WAY) == ()
    )
    hedge_mismatches = store.reconcile(
        observed_flat, position_mode=ExchangePositionMode.HEDGE
    )
    assert len(hedge_mismatches) == 2


def test_close_cannot_consume_another_strategy_quantity(tmp_path) -> None:
    store = VirtualPositionStore(tmp_path / "positions.sqlite3")
    store.initialize()
    _open(
        store,
        event="open-a",
        strategy="a",
        run="run-a",
        side=PositionSide.LONG,
        quantity="1",
        price="100",
    )
    with pytest.raises(VirtualPositionError, match="exceeds"):
        store.close_fill(
            event_id="close-a",
            strategy_id="a",
            run_id="run-a",
            symbol="BTCUSDT",
            position_side=PositionSide.LONG,
            quantity=Decimal("2"),
            price=Decimal("101"),
            commission=Decimal("0"),
            occurred_at=NOW,
        )
    assert store.position("a", "run-a", "BTCUSDT", PositionSide.LONG).quantity == 1


def test_duplicate_fill_is_idempotent_but_conflict_blocks(tmp_path) -> None:
    store = VirtualPositionStore(tmp_path / "positions.sqlite3")
    store.initialize()
    first = _open(
        store,
        event="trade-1",
        strategy="a",
        run="run-a",
        side=PositionSide.LONG,
        quantity="1",
        price="100",
    )
    replay = _open(
        store,
        event="trade-1",
        strategy="a",
        run="run-a",
        side=PositionSide.LONG,
        quantity="1",
        price="100",
    )
    assert replay == first
    with pytest.raises(VirtualPositionError, match="conflicts"):
        _open(
            store,
            event="trade-1",
            strategy="a",
            run="run-a",
            side=PositionSide.LONG,
            quantity="2",
            price="100",
        )


def test_reconciliation_mismatch_is_explicit_and_blocking_input(tmp_path) -> None:
    store = VirtualPositionStore(tmp_path / "positions.sqlite3")
    store.initialize()
    _open(
        store,
        event="open-a",
        strategy="a",
        run="run-a",
        side=PositionSide.LONG,
        quantity="1",
        price="100",
    )
    reasons = store.reconcile(
        (
            FuturesPosition(
                "BTCUSDT", PositionSide.LONG, Decimal("1.1"), Decimal("100")
            ),
        ),
        position_mode=ExchangePositionMode.HEDGE,
    )
    assert len(reasons) == 1
    assert "expected=1" in reasons[0]
    assert "actual=1.1" in reasons[0]
