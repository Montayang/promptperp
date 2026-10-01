from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Mapping

from promptperp.strategy_spec import RuleSpec, StrategySpec, canonical_json

_BASE_FIELDS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "mark_price",
    "funding_rate",
    "rank",
}


class SignalEngineRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class MarketEvent:
    sequence: int
    observed_at: datetime
    symbol: str
    values: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        if self.sequence <= 0 or self.observed_at.tzinfo is None:
            raise ValueError("market event sequence or time is invalid")
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("market event symbol must be uppercase")
        if not self.values or any(
            key not in _BASE_FIELDS or not value.is_finite()
            for key, value in self.values.items()
        ):
            raise ValueError("market event values are invalid")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> MarketEvent:
        if set(value) != {"sequence", "observed_at", "symbol", "values"}:
            raise SignalEngineRejected("market event fields are invalid")
        try:
            observed = datetime.fromisoformat(str(value["observed_at"]))
            raw_values = value["values"]
            if not isinstance(raw_values, dict):
                raise ValueError
            values = {
                str(key): Decimal(item) if isinstance(item, str) else Decimal("NaN")
                for key, item in raw_values.items()
            }
            sequence = value["sequence"]
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                raise ValueError
            return cls(sequence, observed, str(value["symbol"]), values)
        except (ValueError, InvalidOperation) as exc:
            raise SignalEngineRejected("market event encoding is invalid") from exc


@dataclass(frozen=True)
class SignalProposal:
    schema_version: int
    strategy_id: str
    spec_fingerprint: str
    symbol: str
    position_side: str
    action: str
    observed_at: datetime
    sequence: int
    rule_id: str
    reason: str
    take_profit_ratio: Decimal
    stop_loss_ratio: Decimal
    deduplication_key: str

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.action != "OPEN":
            raise ValueError("signal proposal schema or action is invalid")
        if self.position_side not in {"LONG", "SHORT"}:
            raise ValueError("signal proposal side is invalid")
        if self.observed_at.tzinfo is None or self.sequence <= 0:
            raise ValueError("signal proposal time or sequence is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "strategy_id": self.strategy_id,
            "spec_fingerprint": self.spec_fingerprint,
            "symbol": self.symbol,
            "position_side": self.position_side,
            "action": self.action,
            "observed_at": self.observed_at.isoformat(),
            "sequence": self.sequence,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "take_profit_ratio": str(self.take_profit_ratio),
            "stop_loss_ratio": str(self.stop_loss_ratio),
            "deduplication_key": self.deduplication_key,
        }

    def to_json(self) -> bytes:
        return canonical_json(self.to_dict())


@dataclass(frozen=True)
class EngineState:
    schema_version: int = 1
    last_sequence: int = 0
    last_observed_at: datetime | None = None
    cooldown_remaining: int = 0
    histories: Mapping[str, tuple[Decimal, ...]] = MappingProxyType({})

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or self.last_sequence < 0
            or self.cooldown_remaining < 0
        ):
            raise ValueError("engine state is invalid")
        if self.last_observed_at is not None and self.last_observed_at.tzinfo is None:
            raise ValueError("engine state time must be timezone-aware")
        object.__setattr__(
            self,
            "histories",
            MappingProxyType(
                {key: tuple(values) for key, values in self.histories.items()}
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "last_sequence": self.last_sequence,
            "last_observed_at": self.last_observed_at.isoformat()
            if self.last_observed_at
            else None,
            "cooldown_remaining": self.cooldown_remaining,
            "histories": {
                key: [str(value) for value in values]
                for key, values in sorted(self.histories.items())
            },
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EngineState:
        if set(value) != {
            "schema_version",
            "last_sequence",
            "last_observed_at",
            "cooldown_remaining",
            "histories",
        }:
            raise SignalEngineRejected("checkpoint fields are invalid")
        histories = value["histories"]
        if not isinstance(histories, dict) or any(
            not isinstance(items, list) for items in histories.values()
        ):
            raise SignalEngineRejected("checkpoint histories are invalid")
        try:
            observed_raw = value["last_observed_at"]
            observed = (
                datetime.fromisoformat(observed_raw)
                if isinstance(observed_raw, str)
                else None
            )
            return cls(
                schema_version=int(value["schema_version"]),
                last_sequence=int(value["last_sequence"]),
                last_observed_at=observed,
                cooldown_remaining=int(value["cooldown_remaining"]),
                histories={
                    str(key): tuple(Decimal(item) for item in items)
                    for key, items in histories.items()
                },
            )
        except (ValueError, InvalidOperation, TypeError) as exc:
            raise SignalEngineRejected("checkpoint encoding is invalid") from exc


class SignalEngine:
    def __init__(self, spec: StrategySpec, state: EngineState | None = None):
        self.spec = spec
        self.state = state or EngineState()

    def process(
        self, event: MarketEvent, *, accepted_at: datetime
    ) -> tuple[tuple[SignalProposal, ...], EngineState]:
        if accepted_at.tzinfo is None:
            raise ValueError("accepted_at must be timezone-aware")
        expected = self.state.last_sequence + 1
        if event.sequence != expected:
            raise SignalEngineRejected("event is duplicated, missing, or out of order")
        if event.symbol not in self.spec.symbols:
            raise SignalEngineRejected("event symbol is not declared by the spec")
        if (
            self.state.last_observed_at
            and event.observed_at <= self.state.last_observed_at
        ):
            raise SignalEngineRejected("event timestamp is not strictly increasing")
        age = (accepted_at - event.observed_at).total_seconds()
        if age < 0 or age > self.spec.max_age_seconds:
            raise SignalEngineRejected("event is future-dated or stale")

        histories = {key: list(values) for key, values in self.state.histories.items()}
        for field, value in event.values.items():
            history = histories.setdefault(field, [])
            history.append(value)
            maximum = max(
                (item.window for item in self.spec.indicators if item.field == field),
                default=1,
            )
            del history[:-maximum]
        context: dict[str, Decimal | bool] = dict(event.values)
        ready = True
        for indicator in self.spec.indicators:
            values = histories.get(indicator.field, [])
            if len(values) < indicator.window:
                ready = False
                continue
            window = values[-indicator.window :]
            if indicator.kind == "sma":
                result = sum(window, Decimal("0")) / Decimal(len(window))
            elif indicator.kind == "ema":
                alpha = Decimal("2") / Decimal(indicator.window + 1)
                result = window[0]
                for value in window[1:]:
                    result = alpha * value + (Decimal("1") - alpha) * result
            elif indicator.kind == "min":
                result = min(window)
            else:
                result = max(window)
            context[indicator.indicator_id] = result

        cooldown = max(self.state.cooldown_remaining - 1, 0)
        proposals: list[SignalProposal] = []
        if ready and event.sequence > self.spec.warmup and cooldown == 0:
            for rule in self.spec.entry_rules:
                if self._evaluate(rule, context):
                    proposals.append(self._proposal(rule, event))
                    cooldown = self.spec.cooldown_events
                    break
        state = EngineState(
            last_sequence=event.sequence,
            last_observed_at=event.observed_at,
            cooldown_remaining=cooldown,
            histories={key: tuple(values) for key, values in histories.items()},
        )
        self.state = state
        return tuple(proposals), state

    def _proposal(self, rule: RuleSpec, event: MarketEvent) -> SignalProposal:
        identity = {
            "strategy_id": self.spec.strategy_id,
            "spec_fingerprint": self.spec.fingerprint,
            "symbol": event.symbol,
            "sequence": event.sequence,
            "rule_id": rule.rule_id,
        }
        return SignalProposal(
            schema_version=1,
            strategy_id=self.spec.strategy_id,
            spec_fingerprint=self.spec.fingerprint,
            symbol=event.symbol,
            position_side=rule.position_side,
            action="OPEN",
            observed_at=event.observed_at,
            sequence=event.sequence,
            rule_id=rule.rule_id,
            reason=rule.reason,
            take_profit_ratio=self.spec.take_profit_ratio,
            stop_loss_ratio=self.spec.stop_loss_ratio,
            deduplication_key=sha256(canonical_json(identity)).hexdigest(),
        )

    @staticmethod
    def _evaluate(rule: RuleSpec, context: Mapping[str, Decimal | bool]) -> bool:
        value = SignalEngine._expr(rule.expression, context)
        if not isinstance(value, bool):
            raise SignalEngineRejected("entry expression did not produce a boolean")
        return value

    @staticmethod
    def _expr(
        expr: Mapping[str, Any], context: Mapping[str, Decimal | bool]
    ) -> Decimal | bool:
        if "ref" in expr:
            reference = expr["ref"]
            if not isinstance(reference, str) or reference not in context:
                raise SignalEngineRejected("expression input is unavailable")
            return context[reference]
        if "decimal" in expr:
            return Decimal(str(expr["decimal"]))
        if "boolean" in expr:
            return bool(expr["boolean"])
        op = expr["op"]
        args = [SignalEngine._expr(item, context) for item in expr["args"]]
        if op == "not":
            if not isinstance(args[0], bool):
                raise SignalEngineRejected("not requires a boolean")
            return not args[0]
        if op in {"and", "or"}:
            if not all(isinstance(arg, bool) for arg in args):
                raise SignalEngineRejected("logical operator requires booleans")
            return bool(all(args)) if op == "and" else bool(any(args))
        left, right = args
        if isinstance(left, bool) or isinstance(right, bool):
            if op != "eq":
                raise SignalEngineRejected("ordered comparison requires decimals")
        if op == "eq":
            return left == right
        assert isinstance(left, Decimal) and isinstance(right, Decimal)
        if op == "gt":
            return left > right
        if op == "gte":
            return left >= right
        if op == "lt":
            return left < right
        if op == "lte":
            return left <= right
        if op == "add":
            return left + right
        if op == "sub":
            return left - right
        if op == "mul":
            return left * right
        if op == "div":
            if right == 0:
                raise SignalEngineRejected("division by zero")
            return left / right
        raise SignalEngineRejected("expression operator is unsupported")


def proposals_json(proposals: tuple[SignalProposal, ...]) -> bytes:
    return canonical_json([proposal.to_dict() for proposal in proposals])


def state_json(state: EngineState) -> bytes:
    return canonical_json(state.to_dict())


def load_state(payload: bytes) -> EngineState:
    try:
        raw = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SignalEngineRejected("checkpoint is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise SignalEngineRejected("checkpoint root is invalid")
    return EngineState.from_mapping(raw)
