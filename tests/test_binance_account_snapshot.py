from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from promptperp.domain import (
    OrderSide,
    PositionSide,
    RequestUnknown,
    ResponseShapeError,
)
from promptperp.exchange import BinanceAccountSnapshotReader

NOW = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)


class Response:
    def __init__(self, value):
        self.value = value

    def data(self):
        return self.value


class RestAPI:
    def __init__(self):
        self.normal = []
        self.conditional = []

    def get_current_position_mode(self):
        return Response({"dualSidePosition": True})

    def position_information_v3(self):
        return Response(
            [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.25",
                    "positionSide": "LONG",
                    "entryPrice": "100",
                },
                {
                    "symbol": "ETHUSDT",
                    "positionAmt": "0",
                    "positionSide": "SHORT",
                    "entryPrice": "0",
                },
            ]
        )

    def futures_account_balance_v3(self):
        return Response(
            [{"asset": "USDT", "balance": "2500", "availableBalance": "2200"}]
        )

    def current_all_open_orders(self):
        return Response(self.normal)

    def current_all_algo_open_orders(self):
        return Response(self.conditional)


def test_reads_typed_positions_balance_and_both_order_channels() -> None:
    rest = RestAPI()
    rest.normal = [
        {
            "symbol": "BTCUSDT",
            "clientOrderId": "strategy_beta-open-1",
            "side": "BUY",
            "positionSide": "LONG",
            "origQty": "0.10",
        }
    ]
    rest.conditional = [
        {
            "symbol": "BTCUSDT",
            "clientAlgoId": "strategy_alpha-stop-1",
            "side": "SELL",
            "positionSide": "LONG",
            "quantity": "0.25",
        }
    ]

    snapshot = BinanceAccountSnapshotReader(rest_api=rest).read(observed_at=NOW)

    assert snapshot.hedge_mode
    assert snapshot.balance.available_balance == Decimal("2200")
    assert snapshot.positions[0].quantity == Decimal("0.25")
    assert [item.client_order_id for item in snapshot.open_orders] == [
        "strategy_alpha-stop-1",
        "strategy_beta-open-1",
    ]
    assert snapshot.open_orders[1].side is OrderSide.BUY
    assert snapshot.open_orders[1].position_side is PositionSide.LONG
    assert snapshot.open_orders[0].is_conditional


def test_duplicate_client_identity_across_order_channels_fails_closed() -> None:
    rest = RestAPI()
    base = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "positionSide": "LONG",
        "quantity": "1",
    }
    rest.normal = [{**base, "clientOrderId": "duplicate", "origQty": "1"}]
    rest.conditional = [{**base, "clientAlgoId": "duplicate"}]

    with pytest.raises(ResponseShapeError, match="duplicated"):
        BinanceAccountSnapshotReader(rest_api=rest).read(observed_at=NOW)


@pytest.mark.parametrize(
    "row",
    (
        {"symbol": "BTCUSDT", "clientOrderId": "x", "origQty": "1"},
        {
            "symbol": "btcusdt",
            "clientOrderId": "x",
            "side": "BUY",
            "positionSide": "LONG",
            "origQty": "1",
        },
        {
            "symbol": "BTCUSDT",
            "clientOrderId": "x",
            "side": "BUY",
            "positionSide": "LONG",
            "origQty": "0",
        },
    ),
)
def test_malformed_open_order_fails_closed(row) -> None:
    rest = RestAPI()
    rest.normal = [row]

    with pytest.raises(ResponseShapeError):
        BinanceAccountSnapshotReader(rest_api=rest).read(observed_at=NOW)


def test_order_query_failure_is_unknown_not_an_empty_snapshot() -> None:
    rest = RestAPI()

    def fail():
        raise TimeoutError

    rest.current_all_open_orders = fail

    with pytest.raises(RequestUnknown, match="normal open-order query failed"):
        BinanceAccountSnapshotReader(rest_api=rest).read(observed_at=NOW)
