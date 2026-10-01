from __future__ import annotations

from decimal import Decimal

import pytest

from promptperp.config import ExternalEffectBlocked, RuntimeConfig, RuntimeMode
from promptperp.domain import (
    OrderSide,
    OrderStatus,
    PositionSide,
    RequestRejected,
    RequestUnknown,
    ResponseShapeError,
)
from promptperp.exchange import BinanceFuturesAdapter


class Response:
    def __init__(self, value):
        self._value = value

    def data(self):
        return self._value


class Model:
    def __init__(self, **values):
        self._values = values

    def to_dict(self):
        return self._values


class FakeRestAPI:
    def __init__(self):
        self.calls = []
        self.positions = []
        self.balances = []
        self.hedge_mode = True

    def position_information_v3(self):
        self.calls.append(("positions", {}))
        return Response(self.positions)

    def futures_account_balance_v3(self):
        self.calls.append(("balances", {}))
        return Response(self.balances)

    def get_current_position_mode(self):
        self.calls.append(("position_mode", {}))
        return Response({"dualSidePosition": self.hedge_mode})

    def ticker_price(self, **kwargs):
        self.calls.append(("ticker", kwargs))
        return Response({"price": "101.25"})

    def exchange_information(self):
        self.calls.append(("exchange_information", {}))
        return Response(
            {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "filters": [
                            {
                                "filterType": "MARKET_LOT_SIZE",
                                "stepSize": "0.001",
                                "minQty": "0.002",
                            },
                            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    }
                ]
            }
        )

    def new_order(self, **kwargs):
        self.calls.append(("new_order", kwargs))
        return Response(
            {
                "symbol": kwargs["symbol"],
                "orderId": "123",
                "clientOrderId": kwargs["new_client_order_id"],
                "side": kwargs["side"],
                "positionSide": kwargs["position_side"],
                "status": "FILLED",
                "origQty": kwargs["quantity"],
                "executedQty": kwargs["quantity"],
                "avgPrice": "100",
            }
        )

    def query_order(self, **kwargs):
        self.calls.append(("query_order", kwargs))
        return Response(
            {
                "symbol": kwargs["symbol"],
                "orderId": kwargs.get("order_id", "123"),
                "clientOrderId": kwargs.get("orig_client_order_id", "client-1"),
                "side": "BUY",
                "positionSide": "LONG",
                "status": "PARTIALLY_FILLED",
                "origQty": "2",
                "executedQty": "1",
                "avgPrice": "99.5",
            }
        )

    def cancel_order(self, **kwargs):
        self.calls.append(("cancel_order", kwargs))
        return Response({})


def test_positions_are_normalized_and_zero_positions_are_removed():
    rest = FakeRestAPI()
    rest.positions = [
        Model(
            symbol="BTCUSDT",
            positionAmt="0.25",
            positionSide="LONG",
            entryPrice="100",
        ),
        {"symbol": "ETHUSDT", "positionAmt": "0", "positionSide": "SHORT"},
    ]

    positions = BinanceFuturesAdapter(rest_api=rest).list_positions()

    assert len(positions) == 1
    assert positions[0].symbol == "BTCUSDT"
    assert positions[0].side is PositionSide.LONG
    assert positions[0].quantity == Decimal("0.25")


def test_position_query_failure_is_unknown_not_flat():
    class FailingRest:
        def position_information_v3(self):
            raise TimeoutError

    with pytest.raises(RequestUnknown):
        BinanceFuturesAdapter(rest_api=FailingRest()).list_positions()


def test_malformed_active_position_fails_closed():
    rest = FakeRestAPI()
    rest.positions = [{"symbol": "BTCUSDT", "positionAmt": "invalid"}]

    with pytest.raises(ResponseShapeError):
        BinanceFuturesAdapter(rest_api=rest).list_positions()


def test_balance_and_position_mode_are_typed():
    rest = FakeRestAPI()
    rest.balances = [Model(asset="USDT", balance="500", availableBalance="450")]
    adapter = BinanceFuturesAdapter(rest_api=rest)

    balance = adapter.get_balance("usdt")

    assert balance.wallet_balance == Decimal("500")
    assert balance.available_balance == Decimal("450")
    assert adapter.is_hedge_mode() is True


def test_invalid_position_mode_fails_closed():
    rest = FakeRestAPI()
    rest.hedge_mode = "true"

    with pytest.raises(ResponseShapeError):
        BinanceFuturesAdapter(rest_api=rest).is_hedge_mode()


def test_symbol_rules_are_decimal_cached_and_enforced():
    rest = FakeRestAPI()
    adapter = BinanceFuturesAdapter(rest_api=rest)

    rules = adapter.get_symbol_rules("btcusdt")
    cached = adapter.get_symbol_rules("BTCUSDT")

    assert cached is rules
    assert rules.floor_quantity(Decimal("0.1239")) == Decimal("0.123")
    assert rules.floor_price(Decimal("101.29")) == Decimal("101.2")
    rules.validate_notional(Decimal("0.1"), Decimal("100"))
    assert [name for name, _ in rest.calls].count("exchange_information") == 1


def test_last_price_is_typed_decimal():
    adapter = BinanceFuturesAdapter(rest_api=FakeRestAPI())

    assert adapter.get_last_price("BTCUSDT") == Decimal("101.25")


def test_offline_market_order_is_blocked_before_sdk_access():
    rest = FakeRestAPI()
    adapter = BinanceFuturesAdapter(rest_api=rest)

    with pytest.raises(ExternalEffectBlocked, match="disabled by runtime mode"):
        adapter.place_market_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            position_side=PositionSide.LONG,
            quantity=Decimal("0.1"),
            client_order_id="run-1-entry",
        )

    assert rest.calls == []


def test_testnet_market_order_returns_domain_order_and_idempotency_key():
    rest = FakeRestAPI()
    config = RuntimeConfig(
        mode=RuntimeMode.TESTNET,
        external_effects_enabled=True,
        api_key="test-key",
        api_secret="test-secret",
    )
    adapter = BinanceFuturesAdapter(rest_api=rest, runtime_config=config)

    order = adapter.place_market_order(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        quantity=Decimal("0.1"),
        client_order_id="run-1-entry",
    )

    assert order.status is OrderStatus.FILLED
    assert order.client_order_id == "run-1-entry"
    assert rest.calls[-1][1]["new_client_order_id"] == "run-1-entry"


def test_filled_market_order_with_transient_zero_fill_is_reconciled_by_order_id():
    class TransientFillRest(FakeRestAPI):
        def new_order(self, **kwargs):
            response = super().new_order(**kwargs)
            response._value["executedQty"] = "0"
            response._value["avgPrice"] = "0"
            return response

        def query_order(self, **kwargs):
            self.calls.append(("query_order", kwargs))
            return Response(
                {
                    "symbol": kwargs["symbol"],
                    "orderId": kwargs["order_id"],
                    "clientOrderId": "run-1-entry",
                    "side": "BUY",
                    "positionSide": "LONG",
                    "status": "FILLED",
                    "origQty": "0.1",
                    "executedQty": "0.1",
                    "avgPrice": "100",
                }
            )

    rest = TransientFillRest()
    config = RuntimeConfig(
        mode=RuntimeMode.TESTNET,
        external_effects_enabled=True,
        api_key="test-key",
        api_secret="test-secret",
    )
    adapter = BinanceFuturesAdapter(rest_api=rest, runtime_config=config)

    order = adapter.place_market_order(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        quantity=Decimal("0.1"),
        client_order_id="run-1-entry",
    )

    assert order.executed_quantity == Decimal("0.1")
    assert order.average_price == Decimal("100")
    assert rest.calls[-1] == (
        "query_order",
        {"symbol": "BTCUSDT", "order_id": "123"},
    )


def test_filled_market_order_with_persistently_missing_fill_is_unknown():
    class MissingFillRest(FakeRestAPI):
        def new_order(self, **kwargs):
            response = super().new_order(**kwargs)
            response._value["executedQty"] = "0"
            response._value["avgPrice"] = "0"
            return response

        def query_order(self, **kwargs):
            self.calls.append(("query_order", kwargs))
            return Response(
                {
                    "symbol": kwargs["symbol"],
                    "orderId": kwargs["order_id"],
                    "clientOrderId": "run-1-entry",
                    "side": "BUY",
                    "positionSide": "LONG",
                    "status": "FILLED",
                    "origQty": "0.1",
                    "executedQty": "0",
                    "avgPrice": "0",
                }
            )

    config = RuntimeConfig(
        mode=RuntimeMode.TESTNET,
        external_effects_enabled=True,
        api_key="test-key",
        api_secret="test-secret",
    )
    adapter = BinanceFuturesAdapter(rest_api=MissingFillRest(), runtime_config=config)

    with pytest.raises(RequestUnknown, match="execution details remain incomplete"):
        adapter.place_market_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            position_side=PositionSide.LONG,
            quantity=Decimal("0.1"),
            client_order_id="run-1-entry",
        )


def test_write_timeout_is_reported_as_unknown():
    class FailingRest(FakeRestAPI):
        def new_order(self, **kwargs):
            raise TimeoutError

    config = RuntimeConfig(
        mode=RuntimeMode.TESTNET,
        external_effects_enabled=True,
        api_key="test-key",
        api_secret="test-secret",
    )
    adapter = BinanceFuturesAdapter(rest_api=FailingRest(), runtime_config=config)

    with pytest.raises(RequestUnknown, match="outcome is unknown"):
        adapter.place_market_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            position_side=PositionSide.LONG,
            quantity=Decimal("0.1"),
            client_order_id="run-1-entry",
        )


def test_order_query_and_cancel_are_scoped_to_symbol_and_order_id():
    rest = FakeRestAPI()
    config = RuntimeConfig(
        mode=RuntimeMode.TESTNET,
        external_effects_enabled=True,
        api_key="test-key",
        api_secret="test-secret",
    )
    adapter = BinanceFuturesAdapter(rest_api=rest, runtime_config=config)

    order = adapter.get_order(symbol="BTCUSDT", order_id="123")
    adapter.cancel_order(symbol="BTCUSDT", order_id="123")

    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert rest.calls[-1] == (
        "cancel_order",
        {"symbol": "BTCUSDT", "order_id": "123"},
    )


def test_order_can_be_reconciled_by_client_order_id():
    rest = FakeRestAPI()
    adapter = BinanceFuturesAdapter(rest_api=rest)

    order = adapter.get_order(
        symbol="BTCUSDT",
        client_order_id="run-1-entry",
    )

    assert order.client_order_id == "run-1-entry"
    assert rest.calls[-1] == (
        "query_order",
        {"symbol": "BTCUSDT", "orig_client_order_id": "run-1-entry"},
    )


@pytest.mark.parametrize("identifiers", [{}, {"order_id": "1", "client_order_id": "c"}])
def test_order_query_requires_exactly_one_identifier(identifiers):
    with pytest.raises(RequestRejected, match="exactly one"):
        BinanceFuturesAdapter(rest_api=FakeRestAPI()).get_order(
            symbol="BTCUSDT", **identifiers
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"orderId": None},
        {"side": "SIDEWAYS"},
        {"positionSide": None},
        {"executedQty": "3"},
    ],
)
def test_malformed_order_contract_fails_closed(overrides):
    rest = FakeRestAPI()
    original = rest.query_order
    rest.query_order = lambda **kwargs: Response(
        {**original(**kwargs).data(), **overrides}
    )

    with pytest.raises(ResponseShapeError):
        BinanceFuturesAdapter(rest_api=rest).get_order(symbol="BTCUSDT", order_id="123")
