"""Synthetic basket preflight only; no credentials, clients, or live authority."""

import json
from decimal import Decimal

from promptperp.domain import SymbolRules
from promptperp.execution import BasketPolicy, preflight_basket


def main() -> None:
    targets = {
        "AAAUSDT": Decimal("0.25"),
        "BBBUSDT": Decimal("0.25"),
        "CCCUSDT": Decimal("-0.25"),
        "DDDUSDT": Decimal("-0.25"),
    }
    rules = {
        symbol: SymbolRules(
            symbol=symbol,
            quantity_step=Decimal("0.01"),
            minimum_quantity=Decimal("0.01"),
            price_tick=Decimal("0.01"),
            minimum_notional=Decimal("5"),
        )
        for symbol in targets
    }
    basket = preflight_basket(
        target_weights=targets,
        prices={symbol: Decimal("10") for symbol in targets},
        rules=rules,
        policy=BasketPolicy(
            strategy_equity=Decimal("2000"),
            target_count_per_side=2,
            target_weight=Decimal("0.25"),
        ),
        taker_fee_rate=Decimal("0.001"),
    )
    print(
        json.dumps(
            {
                "mode": "offline",
                "execution_permitted": False,
                "synthetic_legs": len(basket.legs),
                "gross_fraction": str(basket.gross_fraction),
                "net_fraction": str(basket.net_fraction),
            }
        )
    )


if __name__ == "__main__":
    main()
