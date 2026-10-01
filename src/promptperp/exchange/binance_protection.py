from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from promptperp.config import RuntimeConfig
from promptperp.domain import (
    OrderSide,
    ProtectionClose,
    RequestRejected,
    RequestUnknown,
    ResponseShapeError,
)
from promptperp.exchange.binance_futures import BinanceFuturesAdapter

if TYPE_CHECKING:
    from promptperp.execution.models import ExecutionSnapshot


class BinanceProtectionAdapter:
    """USD-M conditional stop/take-profit adapter with strict confirmation."""

    _UNSAFE_STATUSES = frozenset({"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"})

    def __init__(
        self,
        *,
        rest_api: Any,
        runtime_config: RuntimeConfig,
        futures: BinanceFuturesAdapter,
        stop_loss_ratio: Decimal,
        take_profit_ratio: Decimal,
    ) -> None:
        if not Decimal("0") < stop_loss_ratio < Decimal("1"):
            raise ValueError("stop-loss ratio must be between zero and one")
        if take_profit_ratio <= 0 or not take_profit_ratio.is_finite():
            raise ValueError("take-profit ratio must be finite and positive")
        self._rest_api = rest_api
        self._runtime_config = runtime_config
        self._futures = futures
        self._stop_loss_ratio = stop_loss_ratio
        self._take_profit_ratio = take_profit_ratio

    def update_runtime_config(self, runtime_config: RuntimeConfig) -> None:
        """Replace the immutable approval after a supervised universe refresh."""

        self._runtime_config = runtime_config

    def place_protection(
        self,
        *,
        snapshot: ExecutionSnapshot,
        stop_client_order_id: str,
        take_profit_client_order_id: str,
    ) -> None:
        stop_price = self._expected_trigger(snapshot, "STOP")
        take_price = self._expected_trigger(snapshot, "TAKE_PROFIT")
        self._place_one(
            snapshot=snapshot,
            role="STOP",
            order_type="STOP_MARKET",
            trigger_price=stop_price,
            client_order_id=stop_client_order_id,
        )
        self._place_one(
            snapshot=snapshot,
            role="TAKE_PROFIT",
            order_type="TAKE_PROFIT_MARKET",
            trigger_price=take_price,
            client_order_id=take_profit_client_order_id,
        )

    def get_protection_order_id(
        self,
        *,
        snapshot: ExecutionSnapshot,
        client_order_id: str,
        role: str,
    ) -> str | None:
        row = self._algo_row(
            symbol=snapshot.intent.symbol,
            client_order_id=client_order_id,
        )
        if row is None:
            return None
        self._validate_algo(row, snapshot=snapshot, role=role)
        algo_id = row.get("algoId", row.get("algo_id"))
        if algo_id in (None, ""):
            raise ResponseShapeError("protection order is missing algo identity")
        return str(algo_id)

    def get_triggered_close(
        self,
        *,
        snapshot: ExecutionSnapshot,
    ) -> ProtectionClose | None:
        triggered: list[ProtectionClose] = []
        for role, client_order_id in (
            ("STOP", snapshot.stop_client_order_id),
            ("TAKE_PROFIT", snapshot.take_profit_client_order_id),
        ):
            if not client_order_id:
                raise ResponseShapeError(
                    "protected snapshot is missing client identity"
                )
            row = self._algo_row(
                symbol=snapshot.intent.symbol,
                client_order_id=client_order_id,
            )
            if row is None:
                raise ResponseShapeError("owned protection order is missing")
            self._validate_algo(row, snapshot=snapshot, role=role)
            actual_id = row.get("actualOrderId", row.get("actual_order_id"))
            if actual_id in (None, "", "0", 0):
                continue
            order = self._futures.get_order(
                symbol=snapshot.intent.symbol,
                order_id=str(actual_id),
            )
            triggered.append(
                ProtectionClose(
                    protection_client_order_id=client_order_id,
                    order=order,
                )
            )
        if len(triggered) > 1:
            raise ResponseShapeError(
                "multiple protection orders claim to have triggered"
            )
        return triggered[0] if triggered else None

    def cancel_open_protection(self, *, snapshot: ExecutionSnapshot) -> None:
        """Cancel only the two conditional orders owned by this execution."""

        trade_api = getattr(self._rest_api, "_tradeApi", None)
        if trade_api is None or not hasattr(trade_api, "cancel_algo_order"):
            raise RequestUnknown("conditional-order cancellation is unavailable")
        for role, client_order_id in (
            ("STOP", snapshot.stop_client_order_id),
            ("TAKE_PROFIT", snapshot.take_profit_client_order_id),
        ):
            if not client_order_id:
                raise ResponseShapeError(
                    "closed execution is missing protection client identity"
                )
            row = self._algo_row(
                symbol=snapshot.intent.symbol,
                client_order_id=client_order_id,
            )
            if row is None:
                raise ResponseShapeError("owned protection order is missing")
            actual_id = row.get("actualOrderId", row.get("actual_order_id"))
            status = str(row.get("algoStatus", row.get("status", ""))).upper()
            if actual_id not in (None, "", "0", 0):
                continue
            if status in {"CANCELED", "CANCELLED", "EXPIRED"}:
                continue
            self._validate_algo(row, snapshot=snapshot, role=role)
            side = self._opposite_side(snapshot.intent.side)
            self._runtime_config.require_account_mutation(
                f"cancel futures {role.lower()} protection",
                symbol=snapshot.intent.symbol,
                side=side.value,
            )
            try:
                trade_api.cancel_algo_order(client_algo_id=client_order_id)
            except Exception as exc:
                raise RequestUnknown(
                    "conditional-order cancellation outcome is unknown"
                ) from exc
            confirmed = self._algo_row(
                symbol=snapshot.intent.symbol,
                client_order_id=client_order_id,
            )
            if confirmed is None:
                raise ResponseShapeError(
                    "canceled protection order cannot be confirmed"
                )
            confirmed_status = str(
                confirmed.get("algoStatus", confirmed.get("status", ""))
            ).upper()
            confirmed_actual = confirmed.get(
                "actualOrderId", confirmed.get("actual_order_id")
            )
            if confirmed_status not in {"CANCELED", "CANCELLED", "EXPIRED"} and (
                confirmed_actual in (None, "", "0", 0)
            ):
                raise RequestUnknown("conditional-order cancellation is not confirmed")

    def _place_one(
        self,
        *,
        snapshot: ExecutionSnapshot,
        role: str,
        order_type: str,
        trigger_price: Decimal,
        client_order_id: str,
    ) -> None:
        side = self._opposite_side(snapshot.intent.side)
        self._runtime_config.require_account_mutation(
            f"place futures {role.lower()} protection",
            symbol=snapshot.intent.symbol,
            side=side.value,
        )
        trade_api = getattr(self._rest_api, "_tradeApi", None)
        if trade_api is None or not hasattr(trade_api, "new_algo_order"):
            raise RequestUnknown("conditional-order API is unavailable")
        try:
            trade_api.new_algo_order(
                algo_type="CONDITIONAL",
                symbol=snapshot.intent.symbol,
                side=side.value,
                type=order_type,
                quantity=str(snapshot.filled_quantity),
                trigger_price=str(trigger_price),
                position_side=snapshot.intent.position_side.value,
                client_algo_id=client_order_id,
            )
        except Exception as exc:
            raise RequestUnknown("conditional-order outcome is unknown") from exc

    def _algo_row(
        self,
        *,
        symbol: str,
        client_order_id: str,
    ) -> dict[str, Any] | None:
        try:
            response = self._rest_api.query_all_algo_orders(
                symbol=symbol,
                limit=100,
            )
        except Exception as exc:
            raise RequestUnknown("conditional-order query failed") from exc
        rows = BinanceFuturesAdapter._rows(response, "conditional-order query")
        matches = [
            BinanceFuturesAdapter._mapping(row, "conditional order")
            for row in rows
            if self._client_id(row) == client_order_id
        ]
        if len(matches) > 1:
            raise ResponseShapeError("conditional-order identity is ambiguous")
        return matches[0] if matches else None

    def _validate_algo(
        self,
        row: dict[str, Any],
        *,
        snapshot: ExecutionSnapshot,
        role: str,
    ) -> None:
        expected_type = "STOP_MARKET" if role == "STOP" else "TAKE_PROFIT_MARKET"
        expected_side = self._opposite_side(snapshot.intent.side).value
        values = {
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "position_side": row.get("positionSide", row.get("position_side")),
            "order_type": row.get("orderType", row.get("type")),
        }
        if values != {
            "symbol": snapshot.intent.symbol,
            "side": expected_side,
            "position_side": snapshot.intent.position_side.value,
            "order_type": expected_type,
        }:
            raise ResponseShapeError("conditional-order protection fields do not match")
        quantity = self._decimal(
            row.get("quantity", row.get("origQty", row.get("orig_qty"))),
            "protection quantity",
        )
        trigger = self._decimal(
            row.get("triggerPrice", row.get("trigger_price")),
            "protection trigger price",
        )
        if quantity != snapshot.filled_quantity:
            raise ResponseShapeError(
                "conditional-order protection quantity does not match"
            )
        if trigger != self._expected_trigger(snapshot, role):
            raise ResponseShapeError("conditional-order trigger price does not match")
        status = row.get("algoStatus", row.get("status"))
        if not isinstance(status, str) or not status:
            raise ResponseShapeError("conditional-order status is missing")
        if status.upper() in self._UNSAFE_STATUSES:
            raise RequestRejected(
                "conditional-order protection is not active or filled"
            )

    def _expected_trigger(
        self,
        snapshot: ExecutionSnapshot,
        role: str,
    ) -> Decimal:
        rules = self._futures.get_symbol_rules(snapshot.intent.symbol)
        multiplier = (
            Decimal("1") - self._stop_loss_ratio
            if role == "STOP"
            else Decimal("1") + self._take_profit_ratio
        )
        return rules.floor_price(snapshot.average_price * multiplier)

    @staticmethod
    def _opposite_side(side: OrderSide) -> OrderSide:
        return OrderSide.SELL if side is OrderSide.BUY else OrderSide.BUY

    @staticmethod
    def _client_id(row: Any) -> str | None:
        data = BinanceFuturesAdapter._mapping(row, "conditional order")
        value = data.get("clientAlgoId", data.get("client_algo_id"))
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _decimal(value: Any, field: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except Exception as exc:
            raise ResponseShapeError(f"{field} is missing or invalid") from exc
        if not result.is_finite() or result <= 0:
            raise ResponseShapeError(f"{field} must be finite and positive")
        return result
