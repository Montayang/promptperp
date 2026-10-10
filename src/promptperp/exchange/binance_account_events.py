from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from promptperp.accounting.exchange_events import ExchangeFunding, ExchangeTrade
from promptperp.domain import (
    Fill,
    FuturesTradeFill,
    OrderSide,
    PositionSide,
    RequestUnknown,
    ResponseShapeError,
)


class BinanceAccountEventReader:
    """Read and normalize Binance USD-M account history without mutating it."""

    MAX_PAGE_SIZE = 1000

    def __init__(self, *, rest_api: Any):
        self._rest_api = rest_api

    def list_trades(
        self, *, symbol: str, start_ms: int, end_ms: int
    ) -> tuple[ExchangeTrade, ...]:
        normalized_symbol, rows = self._trade_rows(
            symbol=symbol, start_ms=start_ms, end_ms=end_ms
        )
        trades = tuple(self._trade(item, normalized_symbol) for item in rows)
        return tuple(sorted(trades, key=lambda item: (item.occurred_at, item.trade_id)))

    def list_fills(
        self, *, symbol: str, start_ms: int, end_ms: int
    ) -> tuple[FuturesTradeFill, ...]:
        normalized_symbol, rows = self._trade_rows(
            symbol=symbol, start_ms=start_ms, end_ms=end_ms
        )
        fills = tuple(self._fill(item, normalized_symbol) for item in rows)
        return tuple(
            sorted(
                fills,
                key=lambda item: (item.occurred_at, item.fill.trade_id),
            )
        )

    def _trade_rows(
        self, *, symbol: str, start_ms: int, end_ms: int
    ) -> tuple[str, tuple[dict[str, Any], ...]]:
        self._validate_window(start_ms, end_ms, maximum_days=7)
        normalized_symbol = symbol.upper()
        first = self._rows(
            self._read(
                "account trade history",
                lambda: self._rest_api.account_trade_list(
                    symbol=normalized_symbol,
                    start_time=start_ms,
                    end_time=end_ms,
                    limit=self.MAX_PAGE_SIZE,
                ),
            ),
            "account trade history",
        )
        rows = [
            item
            for item in first
            if start_ms <= self._required_int(item, "time", "trade") <= end_ms
        ]
        page = first
        seen_ids = {self._required_text(item, "id", "trade") for item in rows}
        while len(page) == self.MAX_PAGE_SIZE:
            last_id = int(self._required_text(page[-1], "id", "trade"))
            page = self._rows(
                self._read(
                    "account trade history continuation",
                    lambda: self._rest_api.account_trade_list(
                        symbol=normalized_symbol,
                        from_id=last_id + 1,
                        limit=self.MAX_PAGE_SIZE,
                    ),
                ),
                "account trade history continuation",
            )
            if page and self._required_int(page[-1], "id", "trade") <= last_id:
                raise ResponseShapeError("account trade pagination did not advance")
            in_window = [
                item
                for item in page
                if start_ms <= self._required_int(item, "time", "trade") <= end_ms
            ]
            for item in in_window:
                trade_id = self._required_text(item, "id", "trade")
                if trade_id not in seen_ids:
                    rows.append(item)
                    seen_ids.add(trade_id)
            if not page or self._required_int(page[-1], "time", "trade") > end_ms:
                break

        return normalized_symbol, tuple(rows)

    def list_funding(
        self, *, start_ms: int, end_ms: int
    ) -> tuple[ExchangeFunding, ...]:
        self._validate_window(start_ms, end_ms, maximum_days=90)
        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        page_number = 1
        while True:
            page = self._rows(
                self._read(
                    "funding income history",
                    lambda: self._rest_api.get_income_history(
                        income_type="FUNDING_FEE",
                        start_time=start_ms,
                        end_time=end_ms,
                        page=page_number,
                        limit=self.MAX_PAGE_SIZE,
                    ),
                ),
                "funding income history",
            )
            page_ids = {self._required_text(item, "tranId", "funding") for item in page}
            if page and page_ids <= seen_ids:
                raise ResponseShapeError("funding pagination did not advance")
            rows.extend(
                item
                for item in page
                if self._required_text(item, "tranId", "funding") not in seen_ids
            )
            seen_ids.update(page_ids)
            if len(page) < self.MAX_PAGE_SIZE:
                break
            page_number += 1
        values = tuple(self._funding(item) for item in rows)
        return tuple(
            sorted(values, key=lambda item: (item.occurred_at, item.transaction_id))
        )

    def resolve_order(self, *, symbol: str, client_order_id: str) -> str | None:
        value = self._mapping(
            self._response_data(
                self._read(
                    "order identity recovery",
                    lambda: self._rest_api.query_order(
                        symbol=symbol.upper(),
                        orig_client_order_id=client_order_id,
                    ),
                )
            ),
            "order identity recovery",
        )
        order_id = value.get("orderId")
        if value.get("clientOrderId") != client_order_id:
            raise ResponseShapeError("recovered order client identity does not match")
        return None if order_id in (None, "") else str(order_id)

    def resolve_algo_order(
        self,
        *,
        symbol: str,
        client_order_id: str,
        algo_id: str | None,
    ) -> tuple[str | None, str | None]:
        rows = self._rows(
            self._read(
                "algo order history",
                lambda: self._rest_api.query_all_algo_orders(
                    symbol=symbol.upper(),
                    algo_id=None if algo_id is None else int(algo_id),
                    limit=100,
                ),
            ),
            "algo order history",
        )
        matches = [
            item
            for item in rows
            if self._required_text(item, "clientAlgoId", "algo order")
            == client_order_id
        ]
        if len(matches) != 1:
            raise ResponseShapeError("algo order identity is missing or ambiguous")
        resolved_algo_id = self._required_text(matches[0], "algoId", "algo order")
        if algo_id is not None and resolved_algo_id != algo_id:
            raise ResponseShapeError("recovered algo identity does not match")
        actual = matches[0].get("actualOrderId")
        if actual in (None, "", "0"):
            return resolved_algo_id, None
        return resolved_algo_id, str(actual)

    def account_equity(self) -> Decimal:
        value = self._mapping(
            self._response_data(
                self._read("account information", self._rest_api.account_information_v3)
            ),
            "account information",
        )
        return self._decimal(value.get("totalMarginBalance"), "account equity")

    def _trade(self, value: dict[str, Any], expected_symbol: str) -> ExchangeTrade:
        symbol = self._required_text(value, "symbol", "trade").upper()
        if symbol != expected_symbol:
            raise ResponseShapeError("trade history returned an unexpected symbol")
        return ExchangeTrade(
            symbol=symbol,
            trade_id=self._required_text(value, "id", "trade"),
            order_id=self._required_text(value, "orderId", "trade"),
            occurred_at=self._timestamp(value, "time", "trade"),
            realized_pnl=self._decimal(value.get("realizedPnl"), "realized PnL"),
            commission=self._decimal(value.get("commission"), "commission"),
            commission_asset=self._required_text(
                value, "commissionAsset", "trade"
            ).upper(),
        )

    def _fill(self, value: dict[str, Any], expected_symbol: str) -> FuturesTradeFill:
        symbol = self._required_text(value, "symbol", "trade").upper()
        if symbol != expected_symbol:
            raise ResponseShapeError("trade history returned an unexpected symbol")
        try:
            side = OrderSide(self._required_text(value, "side", "trade").upper())
            position_side = PositionSide(
                self._required_text(value, "positionSide", "trade").upper()
            )
        except ValueError as exc:
            raise ResponseShapeError(
                "trade side or position side is unsupported"
            ) from exc
        return FuturesTradeFill(
            fill=Fill(
                symbol=symbol,
                trade_id=self._required_text(value, "id", "trade"),
                order_id=self._required_text(value, "orderId", "trade"),
                side=side,
                position_side=position_side,
                quantity=self._positive_decimal(value.get("qty"), "trade quantity"),
                price=self._positive_decimal(value.get("price"), "trade price"),
                commission=self._decimal(value.get("commission"), "commission"),
                commission_asset=self._required_text(
                    value, "commissionAsset", "trade"
                ).upper(),
            ),
            occurred_at=self._timestamp(value, "time", "trade"),
            realized_pnl=self._decimal(value.get("realizedPnl"), "realized PnL"),
        )

    def _funding(self, value: dict[str, Any]) -> ExchangeFunding:
        if self._required_text(value, "incomeType", "funding") != "FUNDING_FEE":
            raise ResponseShapeError("income history returned a non-funding row")
        return ExchangeFunding(
            symbol=self._required_text(value, "symbol", "funding").upper(),
            transaction_id=self._required_text(value, "tranId", "funding"),
            occurred_at=self._timestamp(value, "time", "funding"),
            amount=self._decimal(value.get("income"), "funding income"),
            asset=self._required_text(value, "asset", "funding").upper(),
        )

    @staticmethod
    def _validate_window(start_ms: int, end_ms: int, *, maximum_days: int) -> None:
        if start_ms < 0 or end_ms < start_ms:
            raise ValueError("event history window is invalid")
        if end_ms - start_ms > maximum_days * 24 * 60 * 60 * 1000:
            raise ValueError("event history window exceeds the endpoint limit")

    @staticmethod
    def _response_data(response: Any) -> Any:
        value = getattr(response, "data", response)
        return value() if callable(value) else value

    @classmethod
    def _rows(cls, response: Any, context: str) -> list[dict[str, Any]]:
        value = cls._response_data(response)
        if value is None:
            raise ResponseShapeError(f"{context} returned no data")
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        if not isinstance(value, list):
            value = [value]
        return [cls._mapping(item, context) for item in value]

    @staticmethod
    def _mapping(value: Any, context: str) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if hasattr(value, "to_dict"):
            converted = value.to_dict()
            if isinstance(converted, dict):
                return converted
        raise ResponseShapeError(f"{context} has an unexpected response shape")

    @staticmethod
    def _required_text(value: dict[str, Any], field: str, context: str) -> str:
        raw = value.get(field)
        if raw in (None, ""):
            raise ResponseShapeError(f"{context} is missing {field}")
        return str(raw)

    @classmethod
    def _required_int(cls, value: dict[str, Any], field: str, context: str) -> int:
        try:
            return int(cls._required_text(value, field, context))
        except ValueError as exc:
            raise ResponseShapeError(f"{context} has invalid {field}") from exc

    @classmethod
    def _timestamp(cls, value: dict[str, Any], field: str, context: str) -> datetime:
        milliseconds = cls._required_int(value, field, context)
        try:
            return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise ResponseShapeError(f"{context} has invalid {field}") from exc

    @staticmethod
    def _decimal(value: Any, field: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except Exception as exc:
            raise ResponseShapeError(f"{field} is missing or invalid") from exc
        if not result.is_finite():
            raise ResponseShapeError(f"{field} is not finite")
        return result

    @classmethod
    def _positive_decimal(cls, value: Any, field: str) -> Decimal:
        result = cls._decimal(value, field)
        if result <= 0:
            raise ResponseShapeError(f"{field} must be positive")
        return result

    @staticmethod
    def _read(context: str, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except (RequestUnknown, ResponseShapeError):
            raise
        except Exception as exc:
            raise RequestUnknown(f"{context} failed") from exc
