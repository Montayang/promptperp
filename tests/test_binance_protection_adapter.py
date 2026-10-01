from __future__ import annotations

from decimal import Decimal

import pytest

from promptperp.config import RuntimeConfig, RuntimeMode
from promptperp.domain import (
    OrderSide,
    PositionSide,
    RequestUnknown,
    ResponseShapeError,
)
from promptperp.exchange import BinanceFuturesAdapter, BinanceProtectionAdapter
from promptperp.execution import ExecutionSnapshot, ExecutionState, TradeIntent


class Response:
    def __init__(self, value):
        self._value = value

    def data(self):
        return self._value


class ProtectionRest:
    def __init__(self):
        self._tradeApi = self
        self.algo_rows = []
        self.algo_calls = []
        self.actual_orders = {}
        self.cancel_calls = []

    def exchange_information(self):
        return Response(
            {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "filters": [
                            {
                                "filterType": "MARKET_LOT_SIZE",
                                "stepSize": "0.001",
                                "minQty": "0.001",
                            },
                            {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    }
                ]
            }
        )

    def new_algo_order(self, **kwargs):
        self.algo_calls.append(kwargs)
        row = {
            "algoId": str(9000 + len(self.algo_calls)),
            "clientAlgoId": kwargs["client_algo_id"],
            "symbol": kwargs["symbol"],
            "side": kwargs["side"],
            "positionSide": kwargs["position_side"],
            "orderType": kwargs["type"],
            "quantity": kwargs["quantity"],
            "triggerPrice": kwargs["trigger_price"],
            "algoStatus": "NEW",
            "actualOrderId": "0",
        }
        self.algo_rows.append(row)
        return Response({"algoId": row["algoId"]})

    def query_all_algo_orders(self, **_kwargs):
        return Response(self.algo_rows)

    def query_order(self, **kwargs):
        return Response(self.actual_orders[str(kwargs["order_id"])])

    def cancel_algo_order(self, **kwargs):
        self.cancel_calls.append(kwargs)
        client_id = kwargs["client_algo_id"]
        for row in self.algo_rows:
            if row["clientAlgoId"] == client_id:
                row["algoStatus"] = "CANCELED"
                return Response(row)
        raise AssertionError("unknown conditional order")


def runtime_config():
    return RuntimeConfig(
        mode=RuntimeMode.TESTNET,
        external_effects_enabled=True,
        api_key="test-key",
        api_secret="test-secret",
    )


def snapshot(state=ExecutionState.FILLED):
    values = {
        "intent": TradeIntent(
            strategy_id="sample",
            run_id="run-1",
            intent_id="intent-1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            position_side=PositionSide.LONG,
            quantity=Decimal("2"),
        ),
        "state": state,
        "entry_client_order_id": "entry-1",
        "exchange_order_id": "order-1",
        "filled_quantity": Decimal("2"),
        "average_price": Decimal("100"),
    }
    if state is ExecutionState.PROTECTED:
        values.update(
            {
                "stop_client_order_id": "stop-1",
                "take_profit_client_order_id": "take-1",
                "stop_exchange_order_id": "9001",
                "take_profit_exchange_order_id": "9002",
            }
        )
    if state is ExecutionState.CLOSED:
        values.update(
            {
                "stop_client_order_id": "stop-1",
                "take_profit_client_order_id": "take-1",
                "stop_exchange_order_id": "9001",
                "take_profit_exchange_order_id": "9002",
                "close_client_order_id": "exit-1",
                "close_exchange_order_id": "close-1",
                "close_filled_quantity": Decimal("2"),
                "close_average_price": Decimal("101"),
            }
        )
    return ExecutionSnapshot(**values)


def adapter(rest):
    futures = BinanceFuturesAdapter(rest_api=rest, runtime_config=runtime_config())
    return BinanceProtectionAdapter(
        rest_api=rest,
        runtime_config=runtime_config(),
        futures=futures,
        stop_loss_ratio=Decimal("0.005"),
        take_profit_ratio=Decimal("0.010"),
    )


def test_places_and_strictly_confirms_both_protection_orders():
    rest = ProtectionRest()
    protection = adapter(rest)
    filled = snapshot()

    protection.place_protection(
        snapshot=filled,
        stop_client_order_id="stop-1",
        take_profit_client_order_id="take-1",
    )

    assert [call["type"] for call in rest.algo_calls] == [
        "STOP_MARKET",
        "TAKE_PROFIT_MARKET",
    ]
    assert [call["trigger_price"] for call in rest.algo_calls] == ["99.5", "101.0"]
    assert (
        protection.get_protection_order_id(
            snapshot=filled,
            client_order_id="stop-1",
            role="STOP",
        )
        == "9001"
    )
    assert (
        protection.get_protection_order_id(
            snapshot=filled,
            client_order_id="take-1",
            role="TAKE_PROFIT",
        )
        == "9002"
    )


def test_confirmation_rejects_wrong_quantity_or_trigger():
    rest = ProtectionRest()
    protection = adapter(rest)
    filled = snapshot()
    protection.place_protection(
        snapshot=filled,
        stop_client_order_id="stop-1",
        take_profit_client_order_id="take-1",
    )
    rest.algo_rows[0]["quantity"] = "999"

    with pytest.raises(ResponseShapeError, match="quantity"):
        protection.get_protection_order_id(
            snapshot=filled,
            client_order_id="stop-1",
            role="STOP",
        )


def test_triggered_protection_resolves_actual_filled_close_order():
    rest = ProtectionRest()
    protection = adapter(rest)
    filled = snapshot()
    protection.place_protection(
        snapshot=filled,
        stop_client_order_id="stop-1",
        take_profit_client_order_id="take-1",
    )
    rest.algo_rows[1]["algoStatus"] = "TRIGGERED"
    rest.algo_rows[1]["actualOrderId"] = "close-42"
    rest.actual_orders["close-42"] = {
        "symbol": "BTCUSDT",
        "orderId": "close-42",
        "clientOrderId": "exchange-generated-client",
        "side": "SELL",
        "positionSide": "LONG",
        "status": "FILLED",
        "origQty": "2",
        "executedQty": "2",
        "avgPrice": "105.5",
    }

    triggered = protection.get_triggered_close(
        snapshot=snapshot(ExecutionState.PROTECTED)
    )

    assert triggered is not None
    assert triggered.protection_client_order_id == "take-1"
    assert triggered.order.order_id == "close-42"
    assert triggered.order.average_price == Decimal("105.5")


def test_multiple_claimed_triggers_or_query_failure_is_blocking():
    rest = ProtectionRest()
    protection = adapter(rest)
    filled = snapshot()
    protection.place_protection(
        snapshot=filled,
        stop_client_order_id="stop-1",
        take_profit_client_order_id="take-1",
    )
    for index, row in enumerate(rest.algo_rows, start=1):
        row["actualOrderId"] = f"close-{index}"
        rest.actual_orders[f"close-{index}"] = {
            "symbol": "BTCUSDT",
            "orderId": f"close-{index}",
            "clientOrderId": f"generated-{index}",
            "side": "SELL",
            "positionSide": "LONG",
            "status": "FILLED",
            "origQty": "2",
            "executedQty": "2",
            "avgPrice": "100",
        }

    with pytest.raises(ResponseShapeError, match="multiple"):
        protection.get_triggered_close(snapshot=snapshot(ExecutionState.PROTECTED))

    class FailingRest(ProtectionRest):
        def query_all_algo_orders(self, **_kwargs):
            raise TimeoutError

    with pytest.raises(RequestUnknown, match="query failed"):
        adapter(FailingRest()).get_protection_order_id(
            snapshot=filled,
            client_order_id="stop-1",
            role="STOP",
        )


def test_cleanup_cancels_only_owned_untriggered_protection_orders():
    rest = ProtectionRest()
    protection = adapter(rest)
    filled = snapshot()
    protection.place_protection(
        snapshot=filled,
        stop_client_order_id="stop-1",
        take_profit_client_order_id="take-1",
    )
    rest.algo_rows[1]["algoStatus"] = "TRIGGERED"
    rest.algo_rows[1]["actualOrderId"] = "close-42"

    protection.cancel_open_protection(snapshot=snapshot(ExecutionState.CLOSED))

    assert rest.cancel_calls == [{"client_algo_id": "stop-1"}]
    assert rest.algo_rows[0]["algoStatus"] == "CANCELED"
