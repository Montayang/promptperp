from __future__ import annotations

from decimal import Decimal

import pytest

from promptperp.domain import PositionSide, SymbolRules
from promptperp.execution.basket import (
    BasketAdjustment,
    BasketAdjustmentRole,
    BasketPolicy,
    BasketPreflightError,
    BasketRebalancePlan,
    plan_basket_rebalance,
    plan_safe_flatten,
    preflight_basket,
)


def _targets() -> dict[str, Decimal]:
    weight = Decimal("0.25")
    return {
        **{f"L{index:02d}USDT": weight for index in range(2)},
        **{f"S{index:02d}USDT": -weight for index in range(2)},
    }


def _rules(symbols: set[str], *, step: str = "0.001") -> dict[str, SymbolRules]:
    return {
        symbol: SymbolRules(
            symbol=symbol,
            quantity_step=Decimal(step),
            minimum_quantity=Decimal(step),
            price_tick=Decimal("0.01"),
            minimum_notional=Decimal("5"),
        )
        for symbol in symbols
    }


def test_full_basket_is_sized_from_strategy_equity_and_remains_balanced() -> None:
    targets = _targets()
    prices = {symbol: Decimal("10") for symbol in targets}
    plan = preflight_basket(
        target_weights=targets,
        prices=prices,
        rules=_rules(set(targets)),
        policy=BasketPolicy(
            target_count_per_side=2,
            target_weight=Decimal("0.25"),
            strategy_equity=Decimal("1000"),
        ),
        taker_fee_rate=Decimal("0.0005"),
    )

    assert len(plan.legs) == 4
    assert sum(leg.position_side is PositionSide.LONG for leg in plan.legs) == 2
    assert sum(leg.position_side is PositionSide.SHORT for leg in plan.legs) == 2
    assert plan.long_notional == plan.short_notional
    assert plan.gross_fraction <= Decimal("1")
    assert plan.net_fraction == 0
    assert plan.estimated_taker_fee == plan.gross_notional * Decimal("0.0005")


@pytest.mark.parametrize("missing", ("price", "rules"))
def test_any_missing_market_input_rejects_the_entire_basket(missing: str) -> None:
    targets = _targets()
    prices = {symbol: Decimal("10") for symbol in targets}
    rules = _rules(set(targets))
    symbol = next(iter(targets))
    if missing == "price":
        del prices[symbol]
    else:
        del rules[symbol]

    with pytest.raises(BasketPreflightError, match="basket preflight failed"):
        preflight_basket(
            target_weights=targets,
            prices=prices,
            rules=rules,
            policy=BasketPolicy(
                target_count_per_side=2,
                target_weight=Decimal("0.25"),
                strategy_equity=Decimal("1000"),
            ),
            taker_fee_rate=Decimal("0.0005"),
        )


def test_minimum_notional_failure_does_not_create_a_partial_plan() -> None:
    targets = _targets()
    prices = {symbol: Decimal("1") for symbol in targets}
    rules = _rules(set(targets))
    failed = next(iter(targets))
    rules[failed] = SymbolRules(
        symbol=failed,
        quantity_step=Decimal("1"),
        minimum_quantity=Decimal("1"),
        price_tick=Decimal("0.01"),
        minimum_notional=Decimal("500"),
    )
    with pytest.raises(BasketPreflightError, match=failed):
        preflight_basket(
            target_weights=targets,
            prices=prices,
            rules=rules,
            policy=BasketPolicy(
                target_count_per_side=2,
                target_weight=Decimal("0.25"),
                strategy_equity=Decimal("1000"),
            ),
            taker_fee_rate=Decimal("0.0005"),
        )


def test_unbalanced_rounding_rejects_the_entire_basket() -> None:
    targets = _targets()
    prices = {symbol: Decimal("10") for symbol in targets}
    prices["L00USDT"] = Decimal("31")
    with pytest.raises(BasketPreflightError, match="net exposure"):
        preflight_basket(
            target_weights=targets,
            prices=prices,
            rules=_rules(set(targets), step="1"),
            policy=BasketPolicy(
                target_count_per_side=2,
                target_weight=Decimal("0.25"),
                strategy_equity=Decimal("1000"),
                maximum_net_fraction=Decimal("0.0005"),
            ),
            taker_fee_rate=Decimal("0.0005"),
        )


def test_overweight_side_is_reduced_without_dropping_any_target() -> None:
    targets = _targets()
    prices = {symbol: Decimal("10") for symbol in targets}
    prices["L00USDT"] = Decimal("22.24")
    rules = _rules(set(targets))
    rules["L00USDT"] = SymbolRules(
        symbol="L00USDT",
        quantity_step=Decimal("1"),
        minimum_quantity=Decimal("1"),
        price_tick=Decimal("0.01"),
        minimum_notional=Decimal("5"),
    )
    initial_quantities = {
        symbol: rules[symbol].floor_quantity(
            Decimal("1000") * abs(weight) / prices[symbol]
        )
        for symbol, weight in targets.items()
    }

    plan = preflight_basket(
        target_weights=targets,
        prices=prices,
        rules=rules,
        policy=BasketPolicy(
            target_count_per_side=2,
            target_weight=Decimal("0.25"),
            strategy_equity=Decimal("1000"),
        ),
        taker_fee_rate=Decimal("0.0005"),
    )

    assert len(plan.legs) == 4
    assert abs(plan.net_fraction) <= Decimal("0.01")
    assert all(
        leg.quantity <= initial_quantities[leg.symbol] and leg.quantity > 0
        for leg in plan.legs
    )


def test_rebalance_closes_stale_and_wrong_side_before_new_risk() -> None:
    targets = _targets()
    prices = {symbol: Decimal("10") for symbol in targets}
    target = preflight_basket(
        target_weights=targets,
        prices=prices,
        rules=_rules(set(targets)),
        policy=BasketPolicy(
            target_count_per_side=2,
            target_weight=Decimal("0.25"),
            strategy_equity=Decimal("1000"),
        ),
        taker_fee_rate=Decimal("0.0005"),
    )
    first_long = next(
        leg for leg in target.legs if leg.position_side is PositionSide.LONG
    )
    first_short = next(
        leg for leg in target.legs if leg.position_side is PositionSide.SHORT
    )
    plan = plan_basket_rebalance(
        current_quantities={
            ("STALEUSDT", PositionSide.LONG): Decimal("4"),
            (first_long.symbol, PositionSide.SHORT): Decimal("2"),
            (first_short.symbol, PositionSide.SHORT): first_short.quantity,
        },
        target=target,
    )

    assert plan.ordered == plan.reductions + plan.increases
    assert all(item.role is BasketAdjustmentRole.REDUCE for item in plan.reductions)
    assert all(item.role is BasketAdjustmentRole.INCREASE for item in plan.increases)
    assert {
        (item.symbol, item.position_side, item.target_quantity)
        for item in plan.reductions
    } >= {
        ("STALEUSDT", PositionSide.LONG, Decimal("0")),
        (first_long.symbol, PositionSide.SHORT, Decimal("0")),
    }
    assert not any(
        item.symbol == first_short.symbol and item.position_side is PositionSide.SHORT
        for item in plan.ordered
    )


def test_rebalance_uses_only_supplied_strategy_owned_quantities() -> None:
    targets = _targets()
    prices = {symbol: Decimal("10") for symbol in targets}
    target = preflight_basket(
        target_weights=targets,
        prices=prices,
        rules=_rules(set(targets)),
        policy=BasketPolicy(
            target_count_per_side=2,
            target_weight=Decimal("0.25"),
            strategy_equity=Decimal("1000"),
        ),
        taker_fee_rate=Decimal("0.0005"),
    )
    leg = target.legs[0]
    plan = plan_basket_rebalance(
        current_quantities={(leg.symbol, leg.position_side): leg.quantity},
        target=target,
    )

    assert not any(
        item.symbol == leg.symbol and item.position_side is leg.position_side
        for item in plan.ordered
    )
    assert len(plan.increases) == len(target.legs) - 1


def test_rebalance_phase_cannot_mislabel_an_increase_as_a_reduction() -> None:
    increase = BasketAdjustment(
        role=BasketAdjustmentRole.INCREASE,
        symbol="BTCUSDT",
        position_side=PositionSide.LONG,
        quantity=Decimal("1"),
        target_quantity=Decimal("1"),
    )
    with pytest.raises(ValueError, match="wrong execution phase"):
        BasketRebalancePlan(reductions=(increase,), increases=())


def test_safe_flatten_contains_only_strategy_owned_reductions() -> None:
    plan = plan_safe_flatten(
        current_quantities={
            ("BTCUSDT", PositionSide.LONG): Decimal("1.2"),
            ("BTCUSDT", PositionSide.SHORT): Decimal("0.4"),
            ("ETHUSDT", PositionSide.LONG): Decimal("0"),
        }
    )
    assert plan.increases == ()
    assert [
        (item.symbol, item.position_side, item.quantity, item.target_quantity)
        for item in plan.reductions
    ] == [
        ("BTCUSDT", PositionSide.LONG, Decimal("1.2"), Decimal("0")),
        ("BTCUSDT", PositionSide.SHORT, Decimal("0.4"), Decimal("0")),
    ]


def test_incomplete_or_wrong_weight_contract_is_rejected() -> None:
    targets = _targets()
    targets.pop(next(iter(targets)))
    with pytest.raises(BasketPreflightError, match="full balanced"):
        preflight_basket(
            target_weights=targets,
            prices={symbol: Decimal("10") for symbol in targets},
            rules=_rules(set(targets)),
            policy=BasketPolicy(
                target_count_per_side=2,
                target_weight=Decimal("0.25"),
                strategy_equity=Decimal("1000"),
            ),
            taker_fee_rate=Decimal("0.0005"),
        )
