from decimal import Decimal

import pytest
from binance_common.errors import BadRequestError
from test_binance_futures_adapter import FakeRestAPI, Response

from promptperp.config import RuntimeConfig, RuntimeMode
from promptperp.domain import OrderSide, PositionSide, RequestRejected, RequestUnknown
from promptperp.exchange import BinanceFuturesAdapter


class Lagging(FakeRestAPI):
    failures = 1
    error_code = -2013
    override = None

    def new_order(self, **kwargs):
        result = super().new_order(**kwargs)
        result._value.update(executedQty="0", avgPrice="0")
        return result

    def query_order(self, **kwargs):
        self.calls.append(("query_order", kwargs))
        if self.failures:
            self.failures -= 1
            raise BadRequestError("synthetic query failure", self.error_code)
        result = dict(
            symbol="BTCUSDT",
            orderId="123",
            clientOrderId="example-entry",
            side="BUY",
            positionSide="LONG",
            status="FILLED",
            origQty="0.1",
            executedQty="0.1",
            avgPrice="100",
        )
        result.update(self.override or {})
        return Response(result)


def submit(rest, monkeypatch):
    sleeps = []
    monkeypatch.setattr("promptperp.exchange.binance_futures.time.sleep", sleeps.append)
    adapter = BinanceFuturesAdapter(
        rest_api=rest,
        runtime_config=RuntimeConfig(
            mode=RuntimeMode.TESTNET,
            external_effects_enabled=True,
            api_key="test",
            api_secret="test",
        ),
    )
    order = adapter.place_market_order(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        quantity=Decimal("0.1"),
        client_order_id="example-entry",
    )
    return order, sleeps


def test_index_lag_uses_client_id_and_never_resends(monkeypatch):
    rest = Lagging()
    rest.failures = 6
    order, sleeps = submit(rest, monkeypatch)
    assert order.executed_quantity == Decimal("0.1")
    assert sum(sleeps) == 14.5
    assert sum(name == "new_order" for name, _ in rest.calls) == 1
    assert sum(name == "query_order" for name, _ in rest.calls) == 7
    assert rest.calls[1][1] == {"symbol": "BTCUSDT", "order_id": "123"}
    assert rest.calls[-1][1] == {
        "symbol": "BTCUSDT",
        "orig_client_order_id": "example-entry",
    }


@pytest.mark.parametrize("code,reads", [(-2013, 7), (429, 1), (500, 1), (400, 1)])
def test_exhaustion_or_other_errors_stay_unknown(monkeypatch, code, reads):
    rest = Lagging()
    rest.failures, rest.error_code = 9, code
    with pytest.raises(RequestUnknown):
        submit(rest, monkeypatch)
    assert sum(name == "new_order" for name, _ in rest.calls) == 1
    assert sum(name == "query_order" for name, _ in rest.calls) == reads


@pytest.mark.parametrize(
    "override",
    [
        {"symbol": "ETHUSDT"},
        {"orderId": "124"},
        {"clientOrderId": "other"},
        {"side": "SELL"},
        {"positionSide": "SHORT"},
        {"origQty": "0.2", "executedQty": "0.2"},
    ],
)
def test_wrong_identity_is_never_accepted(monkeypatch, override):
    rest = Lagging()
    rest.failures, rest.override = 0, override
    with pytest.raises(RequestUnknown):
        submit(rest, monkeypatch)
    assert sum(name == "query_order" for name, _ in rest.calls) == 1


def test_read_transport_failure_is_not_retried(monkeypatch):
    class Timeout(Lagging):
        def query_order(self, **kwargs):
            self.calls.append(("query_order", kwargs))
            raise TimeoutError("synthetic")

    rest = Timeout()
    with pytest.raises(RequestUnknown):
        submit(rest, monkeypatch)
    assert [name for name, _ in rest.calls] == ["new_order", "query_order"]


@pytest.mark.parametrize("fields", [{"executedQty": "0"}, {"avgPrice": "0"}])
def test_incomplete_queries_are_retried_without_constructing_invalid_order(
    monkeypatch, fields
):
    class Incomplete(Lagging):
        failures = 0

        def query_order(self, **kwargs):
            result = super().query_order(**kwargs)
            if sum(name == "query_order" for name, _ in self.calls) < 7:
                result._value.update(fields)
            return result

    rest = Incomplete()
    assert submit(rest, monkeypatch)[0].executed_quantity == Decimal("0.1")
    assert sum(name == "query_order" for name, _ in rest.calls) == 7
    assert sum(name == "new_order" for name, _ in rest.calls) == 1


@pytest.mark.parametrize(
    "message,code",
    [
        ("Margin is insufficient.", -2019),
        ("Balance is insufficient.", -2018),
        ("Precision is over the maximum defined for this asset.", -1111),
    ],
)
def test_current_sdk_numeric_rejections_are_definitive(monkeypatch, message, code):
    class Rejected(FakeRestAPI):
        def new_order(self, **kwargs):
            self.calls.append(("new_order", kwargs))
            raise BadRequestError(message, code)

    rest = Rejected()
    with pytest.raises(RequestRejected):
        submit(rest, monkeypatch)
    assert [name for name, _ in rest.calls] == ["new_order"]
