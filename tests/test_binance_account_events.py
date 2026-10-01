from __future__ import annotations

from decimal import Decimal

import pytest

from promptperp.exchange import BinanceAccountEventReader


class Response:
    def __init__(self, value):
        self.value = value

    def data(self):
        return self.value


class RestApi:
    def __init__(self):
        self.trade_calls = []
        self.income_calls = []

    def account_trade_list(self, **kwargs):
        self.trade_calls.append(kwargs)
        return Response(
            [
                {
                    "symbol": "BTCUSDT",
                    "id": 7,
                    "orderId": 42,
                    "time": 1_700_000_000_000,
                    "realizedPnl": "12.5",
                    "commission": "0.25",
                    "commissionAsset": "USDT",
                }
            ]
        )

    def get_income_history(self, **kwargs):
        self.income_calls.append(kwargs)
        return Response(
            [
                {
                    "symbol": "BTCUSDT",
                    "incomeType": "FUNDING_FEE",
                    "income": "-0.5",
                    "asset": "USDT",
                    "time": 1_700_000_100_000,
                    "tranId": 99,
                }
            ]
        )


def test_reader_normalizes_trade_and_funding_without_mutation_calls():
    rest_api = RestApi()
    reader = BinanceAccountEventReader(rest_api=rest_api)

    trades = reader.list_trades(
        symbol="btcusdt",
        start_ms=1_700_000_000_000,
        end_ms=1_700_000_200_000,
    )
    funding = reader.list_funding(
        start_ms=1_700_000_000_000,
        end_ms=1_700_000_200_000,
    )

    assert trades[0].event_id == "binance:trade:BTCUSDT:7"
    assert trades[0].realized_pnl == Decimal("12.5")
    assert funding[0].event_id == "binance:funding:99"
    assert funding[0].amount == Decimal("-0.5")
    assert rest_api.trade_calls[0]["symbol"] == "BTCUSDT"
    assert rest_api.income_calls[0]["income_type"] == "FUNDING_FEE"


def test_reader_rejects_non_usdt_commission():
    rest_api = RestApi()
    original = rest_api.account_trade_list

    def non_usdt(**kwargs):
        response = original(**kwargs)
        response.value[0]["commissionAsset"] = "BNB"
        return response

    rest_api.account_trade_list = non_usdt

    with pytest.raises(Exception, match="non-USDT commission"):
        BinanceAccountEventReader(rest_api=rest_api).list_trades(
            symbol="BTCUSDT",
            start_ms=1_700_000_000_000,
            end_ms=1_700_000_200_000,
        )


def test_reader_discards_rows_outside_requested_window():
    trades = BinanceAccountEventReader(rest_api=RestApi()).list_trades(
        symbol="BTCUSDT",
        start_ms=1_700_000_100_000,
        end_ms=1_700_000_200_000,
    )

    assert trades == ()


def test_reader_rejects_oversized_trade_window():
    with pytest.raises(ValueError, match="endpoint limit"):
        BinanceAccountEventReader(rest_api=RestApi()).list_trades(
            symbol="BTCUSDT",
            start_ms=0,
            end_ms=8 * 24 * 60 * 60 * 1000,
        )
