from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
from typing import Any, Mapping, Protocol

from promptperp.runtime.models import SignalProposal, StrategyDescriptor
from promptperp.strategies.threshold_momentum import (
    PriceSnapshot,
    ThresholdMomentumParameters,
    ThresholdMomentumStrategy,
)


class StrategyInstance(Protocol):
    def on_event(self, event: object) -> SignalProposal | None: ...


class StrategyPlugin(Protocol):
    descriptor: StrategyDescriptor

    def normalize_parameters(self, raw: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def migrate_parameters(self, raw: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def create(self, parameters: Mapping[str, Any]) -> StrategyInstance: ...


def parameter_fingerprint(parameters: Mapping[str, Any]) -> str:
    return sha256(
        json.dumps(
            parameters, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()


def _parameter_mapping(
    value: ThresholdMomentumParameters,
) -> dict[str, Any]:
    result = asdict(value)
    for key, item in tuple(result.items()):
        if isinstance(item, Decimal):
            result[key] = str(item)
    return result


def _reject_unknown(values: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = set(values) - allowed
    if unknown:
        raise ValueError("unknown strategy parameters: " + ",".join(sorted(unknown)))


class _MomentumInstance:
    def __init__(self, parameters: ThresholdMomentumParameters):
        self._strategy = ThresholdMomentumStrategy(parameters)

    def on_event(self, event: object) -> SignalProposal | None:
        if not isinstance(event, PriceSnapshot):
            raise TypeError("threshold_momentum requires a PriceSnapshot")
        signal = self._strategy.on_price(event)
        if signal is None:
            return None
        return SignalProposal(
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            side=signal.side,
            position_side=signal.position_side,
            margin=signal.margin,
            leverage=signal.leverage,
            take_profit_ratio=signal.take_profit_ratio,
            stop_loss_ratio=signal.stop_loss_ratio,
            observed_at=signal.observed_at,
            reason=signal.reason,
        )


class ThresholdMomentumPlugin:
    descriptor = StrategyDescriptor(
        strategy_id="threshold_momentum",
        strategy_version="1.0.0",
        interface_version=1,
        parameter_schema_version=1,
        event_kind="price.v1",
        parameter_schema={
            "symbol": {"type": "string", "pattern": "^[A-Z0-9]+$"},
            "threshold_bps": {"type": "decimal", "exclusiveMinimum": "0"},
            "margin": {"type": "decimal", "exclusiveMinimum": "0"},
            "leverage": {"type": "integer", "minimum": 1},
            "take_profit_ratio": {"type": "decimal", "exclusiveMinimum": "0"},
            "stop_loss_ratio": {"type": "decimal", "exclusiveMinimum": "0"},
        },
    )

    def migrate_parameters(self, raw: Mapping[str, Any]) -> Mapping[str, Any]:
        values = dict(raw)
        _reject_unknown(
            values,
            frozenset(
                {
                    "schema_version",
                    "strategy_id",
                    "symbol",
                    "threshold_bps",
                    "margin",
                    "leverage",
                    "take_profit_ratio",
                    "stop_loss_ratio",
                }
            ),
        )
        if int(values.get("schema_version", 1)) != 1:
            raise ValueError("unsupported momentum parameter migration")
        return values

    def normalize_parameters(self, raw: Mapping[str, Any]) -> Mapping[str, Any]:
        values = self.migrate_parameters(raw)
        parameters = ThresholdMomentumParameters(
            schema_version=int(values.get("schema_version", 1)),
            strategy_id=str(values.get("strategy_id", "threshold_momentum")),
            symbol=str(values.get("symbol", "BTCUSDT")).upper(),
            threshold_bps=Decimal(str(values.get("threshold_bps", "25"))),
            margin=Decimal(str(values.get("margin", "100"))),
            leverage=int(values.get("leverage", 2)),
            take_profit_ratio=Decimal(str(values.get("take_profit_ratio", "0.01"))),
            stop_loss_ratio=Decimal(str(values.get("stop_loss_ratio", "0.005"))),
        )
        return _parameter_mapping(parameters)

    def create(self, parameters: Mapping[str, Any]) -> StrategyInstance:
        normalized = self.normalize_parameters(parameters)
        return _MomentumInstance(
            ThresholdMomentumParameters(
                schema_version=int(normalized["schema_version"]),
                strategy_id=str(normalized["strategy_id"]),
                symbol=str(normalized["symbol"]),
                threshold_bps=Decimal(str(normalized["threshold_bps"])),
                margin=Decimal(str(normalized["margin"])),
                leverage=int(normalized["leverage"]),
                take_profit_ratio=Decimal(str(normalized["take_profit_ratio"])),
                stop_loss_ratio=Decimal(str(normalized["stop_loss_ratio"])),
            )
        )


class StrategyPluginRegistry:
    """Explicit registry: discovery never imports arbitrary filesystem code."""

    def __init__(self, plugins: tuple[StrategyPlugin, ...] | None = None):
        selected = plugins or (ThresholdMomentumPlugin(),)
        self._plugins = {plugin.descriptor.strategy_id: plugin for plugin in selected}
        if len(self._plugins) != len(selected):
            raise ValueError("strategy plugin identities must be unique")

    def discover(self) -> tuple[StrategyDescriptor, ...]:
        return tuple(self._plugins[key].descriptor for key in sorted(self._plugins))

    def load(self, strategy_id: str) -> StrategyPlugin:
        try:
            return self._plugins[strategy_id]
        except KeyError as exc:
            raise ValueError("strategy plugin is not registered") from exc
