from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from promptperp.domain import PositionSide, SymbolRules


class BasketPreflightError(RuntimeError):
    pass


class BasketAdjustmentRole(str, Enum):
    REDUCE = "REDUCE"
    INCREASE = "INCREASE"


@dataclass(frozen=True)
class BasketPolicy:
    strategy_equity: Decimal
    target_count_per_side: int
    target_weight: Decimal
    maximum_gross_fraction: Decimal = Decimal("1")
    maximum_net_fraction: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if not self.strategy_equity.is_finite() or self.strategy_equity <= 0:
            raise ValueError("strategy equity must be finite and positive")
        if self.target_count_per_side <= 0:
            raise ValueError("target count per side must be positive")
        decimals = (
            self.target_weight,
            self.maximum_gross_fraction,
            self.maximum_net_fraction,
        )
        if not all(value.is_finite() and value > 0 for value in decimals):
            raise ValueError("basket policy fractions must be finite and positive")
        expected = self.target_weight * Decimal(self.target_count_per_side * 2)
        if expected > self.maximum_gross_fraction:
            raise ValueError("target basket exceeds the gross exposure limit")


@dataclass(frozen=True)
class BasketLeg:
    symbol: str
    position_side: PositionSide
    weight: Decimal
    price: Decimal
    quantity: Decimal
    notional: Decimal


@dataclass(frozen=True)
class BasketPlan:
    strategy_equity: Decimal
    legs: tuple[BasketLeg, ...]
    long_notional: Decimal
    short_notional: Decimal
    gross_notional: Decimal
    net_notional: Decimal
    gross_fraction: Decimal
    net_fraction: Decimal
    estimated_taker_fee: Decimal
    prices: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        object.__setattr__(self, "prices", MappingProxyType(dict(self.prices)))


@dataclass(frozen=True)
class BasketAdjustment:
    role: BasketAdjustmentRole
    symbol: str
    position_side: PositionSide
    quantity: Decimal
    target_quantity: Decimal

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("basket adjustment symbol must be uppercase")
        if self.quantity <= 0 or self.target_quantity < 0:
            raise ValueError("basket adjustment quantities are invalid")


@dataclass(frozen=True)
class BasketRebalancePlan:
    reductions: tuple[BasketAdjustment, ...]
    increases: tuple[BasketAdjustment, ...]

    def __post_init__(self) -> None:
        if any(
            item.role is not BasketAdjustmentRole.REDUCE for item in self.reductions
        ) or any(
            item.role is not BasketAdjustmentRole.INCREASE for item in self.increases
        ):
            raise ValueError("basket rebalance roles are in the wrong execution phase")

    @property
    def ordered(self) -> tuple[BasketAdjustment, ...]:
        return self.reductions + self.increases


def preflight_basket(
    *,
    target_weights: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    rules: Mapping[str, SymbolRules],
    policy: BasketPolicy,
    taker_fee_rate: Decimal,
) -> BasketPlan:
    """Validate and size the entire basket without producing exchange orders."""

    if not taker_fee_rate.is_finite() or taker_fee_rate < 0:
        raise ValueError("taker fee rate must be finite and non-negative")
    longs = [symbol for symbol, weight in target_weights.items() if weight > 0]
    shorts = [symbol for symbol, weight in target_weights.items() if weight < 0]
    if (
        len(longs) != policy.target_count_per_side
        or len(shorts) != policy.target_count_per_side
    ):
        raise BasketPreflightError(
            "target basket must contain the full balanced symbol count"
        )
    if set(longs) & set(shorts):
        raise BasketPreflightError("a symbol cannot be both long and short")

    expected_weight = policy.target_weight
    if any(abs(weight) != expected_weight for weight in target_weights.values()):
        raise BasketPreflightError("target weights do not match the frozen contract")

    legs: list[BasketLeg] = []
    failures: list[str] = []
    for symbol in sorted(target_weights):
        price = prices.get(symbol)
        symbol_rules = rules.get(symbol)
        if price is None or not price.is_finite() or price <= 0:
            failures.append(f"{symbol}:PRICE_UNAVAILABLE")
            continue
        if symbol_rules is None:
            failures.append(f"{symbol}:RULES_UNAVAILABLE")
            continue
        desired_notional = abs(target_weights[symbol]) * policy.strategy_equity
        try:
            quantity = symbol_rules.floor_quantity(desired_notional / price)
            symbol_rules.validate_notional(quantity, price)
        except ValueError:
            failures.append(f"{symbol}:NOT_TRADABLE_AFTER_ROUNDING")
            continue
        notional = quantity * price
        side = PositionSide.LONG if target_weights[symbol] > 0 else PositionSide.SHORT
        legs.append(
            BasketLeg(
                symbol=symbol,
                position_side=side,
                weight=target_weights[symbol],
                price=price,
                quantity=quantity,
                notional=notional,
            )
        )
    if failures:
        raise BasketPreflightError("basket preflight failed: " + ",".join(failures))

    legs = _balance_by_reducing_overweight_side(
        legs,
        rules=rules,
        maximum_net_notional=policy.maximum_net_fraction * policy.strategy_equity,
    )

    long_notional = sum(
        (leg.notional for leg in legs if leg.position_side is PositionSide.LONG),
        Decimal("0"),
    )
    short_notional = sum(
        (leg.notional for leg in legs if leg.position_side is PositionSide.SHORT),
        Decimal("0"),
    )
    gross = long_notional + short_notional
    net = long_notional - short_notional
    gross_fraction = gross / policy.strategy_equity
    net_fraction = net / policy.strategy_equity
    if gross_fraction > policy.maximum_gross_fraction:
        raise BasketPreflightError("rounded basket exceeds gross exposure limit")
    if abs(net_fraction) > policy.maximum_net_fraction:
        raise BasketPreflightError("rounded basket exceeds net exposure tolerance")
    return BasketPlan(
        strategy_equity=policy.strategy_equity,
        legs=tuple(legs),
        long_notional=long_notional,
        short_notional=short_notional,
        gross_notional=gross,
        net_notional=net,
        gross_fraction=gross_fraction,
        net_fraction=net_fraction,
        estimated_taker_fee=gross * taker_fee_rate,
        prices=prices,
    )


def _balance_by_reducing_overweight_side(
    legs: list[BasketLeg],
    *,
    rules: Mapping[str, SymbolRules],
    maximum_net_notional: Decimal,
) -> list[BasketLeg]:
    """Reduce only the heavier side until exchange steps are acceptably balanced."""

    balanced = list(legs)
    for _ in range(len(balanced) * 2):
        long_notional = sum(
            (
                leg.notional
                for leg in balanced
                if leg.position_side is PositionSide.LONG
            ),
            Decimal("0"),
        )
        short_notional = sum(
            (
                leg.notional
                for leg in balanced
                if leg.position_side is PositionSide.SHORT
            ),
            Decimal("0"),
        )
        net = long_notional - short_notional
        if abs(net) <= maximum_net_notional:
            return balanced
        overweight = PositionSide.LONG if net > 0 else PositionSide.SHORT
        candidates: list[tuple[Decimal, str, int, int, BasketLeg]] = []
        for index, leg in enumerate(balanced):
            if leg.position_side is not overweight:
                continue
            rule = rules[leg.symbol]
            minimum_for_notional = (
                rule.minimum_notional / leg.price / rule.quantity_step
            ).to_integral_value(rounding=ROUND_UP) * rule.quantity_step
            minimum_quantity = max(rule.minimum_quantity, minimum_for_notional)
            maximum_steps = int(
                (
                    (leg.quantity - minimum_quantity) / rule.quantity_step
                ).to_integral_value(rounding=ROUND_DOWN)
            )
            if maximum_steps < 1:
                continue
            step_notional = rule.quantity_step * leg.price
            ideal_steps = abs(net) / step_notional
            step_options = {
                max(
                    1,
                    min(
                        maximum_steps,
                        int(ideal_steps.to_integral_value(rounding=ROUND_DOWN)),
                    ),
                ),
                max(
                    1,
                    min(
                        maximum_steps,
                        int(ideal_steps.to_integral_value(rounding=ROUND_UP)),
                    ),
                ),
            }
            for steps in step_options:
                reduction = step_notional * steps
                adjusted_net = (
                    net - reduction
                    if overweight is PositionSide.LONG
                    else net + reduction
                )
                if abs(adjusted_net) >= abs(net):
                    continue
                new_quantity = leg.quantity - rule.quantity_step * steps
                candidates.append(
                    (
                        abs(adjusted_net),
                        leg.symbol,
                        steps,
                        index,
                        BasketLeg(
                            symbol=leg.symbol,
                            position_side=leg.position_side,
                            weight=leg.weight,
                            price=leg.price,
                            quantity=new_quantity,
                            notional=new_quantity * leg.price,
                        ),
                    )
                )
        if not candidates:
            return balanced
        _, _, _, index, replacement = min(candidates, key=lambda item: item[:3])
        balanced[index] = replacement
    return balanced


def plan_basket_rebalance(
    *,
    current_quantities: Mapping[tuple[str, PositionSide], Decimal],
    target: BasketPlan,
) -> BasketRebalancePlan:
    """Plan one strategy's close-first transition to a preflighted basket."""

    current: dict[tuple[str, PositionSide], Decimal] = {}
    for (symbol, side), quantity in current_quantities.items():
        if not symbol or symbol != symbol.upper() or not quantity.is_finite():
            raise ValueError("current basket position is invalid")
        if quantity < 0:
            raise ValueError("current basket quantity cannot be negative")
        if quantity != 0:
            current[(symbol, side)] = quantity

    desired: dict[tuple[str, PositionSide], Decimal] = {}
    for leg in target.legs:
        key = (leg.symbol, leg.position_side)
        if key in desired:
            raise ValueError("target basket contains a duplicate position leg")
        desired[key] = leg.quantity

    reductions: list[BasketAdjustment] = []
    increases: list[BasketAdjustment] = []
    keys = sorted(
        set(current) | set(desired), key=lambda item: (item[0], item[1].value)
    )
    for symbol, side in keys:
        held = current.get((symbol, side), Decimal("0"))
        wanted = desired.get((symbol, side), Decimal("0"))
        difference = wanted - held
        if difference < 0:
            reductions.append(
                BasketAdjustment(
                    role=BasketAdjustmentRole.REDUCE,
                    symbol=symbol,
                    position_side=side,
                    quantity=abs(difference),
                    target_quantity=wanted,
                )
            )
        elif difference > 0:
            increases.append(
                BasketAdjustment(
                    role=BasketAdjustmentRole.INCREASE,
                    symbol=symbol,
                    position_side=side,
                    quantity=difference,
                    target_quantity=wanted,
                )
            )
    # Minimize temporary net exposure while completing the frozen targets.
    # The whole reduction phase still precedes every increase.
    remaining = dict(current)
    for item in reductions:
        remaining[(item.symbol, item.position_side)] = item.target_quantity
    net = sum(
        (
            quantity
            * target.prices[symbol]
            * (Decimal("1") if side is PositionSide.LONG else Decimal("-1"))
            for (symbol, side), quantity in remaining.items()
            if quantity
        ),
        Decimal("0"),
    )
    ordered_increases = []
    while increases:

        def resulting_net(item: BasketAdjustment) -> Decimal:
            direction = (
                Decimal("1")
                if item.position_side is PositionSide.LONG
                else Decimal("-1")
            )
            return net + direction * item.quantity * target.prices[item.symbol]

        item = min(
            increases,
            key=lambda value: (
                abs(resulting_net(value)),
                value.symbol,
                value.position_side.value,
            ),
        )
        net = resulting_net(item)
        ordered_increases.append(item)
        increases.remove(item)
    return BasketRebalancePlan(tuple(reductions), tuple(ordered_increases))


def plan_safe_flatten(
    *, current_quantities: Mapping[tuple[str, PositionSide], Decimal]
) -> BasketRebalancePlan:
    """Create a deterministic reduce-only plan for one strategy's owned lots."""

    reductions: list[BasketAdjustment] = []
    for (symbol, side), quantity in sorted(
        current_quantities.items(), key=lambda item: (item[0][0], item[0][1].value)
    ):
        if not symbol or symbol != symbol.upper():
            raise ValueError("safe-flatten symbol must be uppercase")
        if not quantity.is_finite() or quantity < 0:
            raise ValueError("safe-flatten quantity is invalid")
        if quantity == 0:
            continue
        reductions.append(
            BasketAdjustment(
                role=BasketAdjustmentRole.REDUCE,
                symbol=symbol,
                position_side=side,
                quantity=quantity,
                target_quantity=Decimal("0"),
            )
        )
    return BasketRebalancePlan(reductions=tuple(reductions), increases=())
