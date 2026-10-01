from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Mapping, Sequence

_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SYMBOL = re.compile(r"[A-Z0-9]{2,20}USDT")
_MAX_INPUT_BYTES = 128 * 1024
_MAX_RULES = 32
_MAX_INDICATORS = 32
_MAX_SCENARIOS = 64
_MAX_EXPR_NODES = 128
_MAX_EXPR_DEPTH = 16
_MAX_WINDOW = 4096
_ALLOWED_TOP = {
    "schema_version",
    "interface_version",
    "strategy_id",
    "name",
    "description",
    "data",
    "symbols",
    "indicators",
    "entry_rules",
    "exit_intent",
    "cooldown_events",
    "assumptions",
    "unresolved",
    "assertions",
    "scenarios",
}
_FORBIDDEN_KEYS = {
    "account",
    "account_id",
    "api_key",
    "api_secret",
    "authorization",
    "code",
    "command",
    "credential",
    "email",
    "environment",
    "leverage",
    "margin",
    "order_type",
    "password",
    "path",
    "private_key",
    "secret",
    "smtp",
    "token",
    "url",
}
_OPS = {"and", "or", "not", "gt", "gte", "lt", "lte", "eq", "add", "sub", "mul", "div"}


class StrategySpecRejected(ValueError):
    pass


def _reject_float(_: str) -> None:
    raise StrategySpecRejected("JSON floating-point numbers are forbidden")


def _object_no_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrategySpecRejected(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _decimal(value: object, *, field: str) -> Decimal:
    if not isinstance(value, str):
        raise StrategySpecRejected(f"{field} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise StrategySpecRejected(f"{field} is not a decimal") from exc
    if not parsed.is_finite():
        raise StrategySpecRejected(f"{field} must be finite")
    return parsed


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _normalize_expression(expr: Any) -> Any:
    if not isinstance(expr, dict):
        return expr
    if "decimal" in expr:
        return {
            "decimal": _decimal_text(
                _decimal(expr["decimal"], field="expression decimal")
            )
        }
    if "args" in expr:
        return {
            "op": expr["op"],
            "args": [_normalize_expression(item) for item in expr["args"]],
        }
    return dict(expr)


def _exact_keys(value: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise StrategySpecRejected(f"{field} has unknown fields: {sorted(unknown)}")


def _scan_forbidden(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = key.lower()
            if lowered in _FORBIDDEN_KEYS:
                raise StrategySpecRejected(f"forbidden capability field: {key}")
            _scan_forbidden(item)
    elif isinstance(value, list):
        for item in value:
            _scan_forbidden(item)
    elif isinstance(value, str):
        lowered = value.lower()
        unsafe = ("http://", "https://", "file://", "../", "/home/", "-----begin")
        if any(marker in lowered for marker in unsafe) or "\x00" in value:
            raise StrategySpecRejected(
                "spec contains a forbidden path, URL, or key material"
            )


def _validate_expr(expr: object, *, refs: set[str], depth: int = 1) -> int:
    if depth > _MAX_EXPR_DEPTH or not isinstance(expr, dict):
        raise StrategySpecRejected("expression is malformed or too deep")
    _exact_keys(expr, {"op", "args", "ref", "decimal", "boolean"}, "expression")
    terminal = [name for name in ("ref", "decimal", "boolean") if name in expr]
    if terminal:
        if len(terminal) != 1 or len(expr) != 1:
            raise StrategySpecRejected("expression terminal is ambiguous")
        kind = terminal[0]
        value = expr[kind]
        if kind == "ref" and (not isinstance(value, str) or value not in refs):
            raise StrategySpecRejected("expression reference is not declared")
        if kind == "decimal":
            _decimal(value, field="expression decimal")
        if kind == "boolean" and not isinstance(value, bool):
            raise StrategySpecRejected("expression boolean is invalid")
        return 1
    if set(expr) != {"op", "args"} or expr["op"] not in _OPS:
        raise StrategySpecRejected("expression operator is unsupported")
    args = expr["args"]
    if not isinstance(args, list):
        raise StrategySpecRejected("expression args must be a list")
    expected = 1 if expr["op"] == "not" else 2
    if len(args) != expected:
        raise StrategySpecRejected("expression operator arity is invalid")
    nodes = 1 + sum(_validate_expr(arg, refs=refs, depth=depth + 1) for arg in args)
    if nodes > _MAX_EXPR_NODES:
        raise StrategySpecRejected("expression exceeds the node limit")
    return nodes


@dataclass(frozen=True)
class IndicatorSpec:
    indicator_id: str
    kind: str
    field: str
    window: int


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    position_side: str
    reason: str
    expression: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "expression", MappingProxyType(dict(self.expression)))


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    category: str
    events: tuple[Mapping[str, Any], ...]
    expect_signal: bool
    expect_error: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "events",
            tuple(MappingProxyType(dict(event)) for event in self.events),
        )


@dataclass(frozen=True)
class StrategySpec:
    schema_version: int
    interface_version: int
    strategy_id: str
    name: str
    description: str
    event_kind: str
    interval: str
    max_age_seconds: int
    warmup: int
    symbols: tuple[str, ...]
    indicators: tuple[IndicatorSpec, ...]
    entry_rules: tuple[RuleSpec, ...]
    take_profit_ratio: Decimal
    stop_loss_ratio: Decimal
    cooldown_events: int
    assumptions: tuple[str, ...]
    unresolved: tuple[str, ...]
    assertions: tuple[str, ...]
    scenarios: tuple[ScenarioSpec, ...]
    normalized: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "normalized", MappingProxyType(dict(self.normalized)))

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json(dict(self.normalized))

    @property
    def fingerprint(self) -> str:
        return sha256(self.canonical_bytes).hexdigest()


def _string_list(value: object, field: str, *, maximum: int = 64) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise StrategySpecRejected(f"{field} must be a bounded list")
    if any(not isinstance(item, str) or not item or len(item) > 500 for item in value):
        raise StrategySpecRejected(f"{field} contains an invalid string")
    return tuple(value)


def load_strategy_spec(payload: bytes) -> StrategySpec:
    if len(payload) > _MAX_INPUT_BYTES:
        raise StrategySpecRejected("spec exceeds the input size limit")
    try:
        raw = json.loads(
            payload.decode("utf-8"),
            parse_float=_reject_float,
            parse_constant=_reject_float,
            object_pairs_hook=_object_no_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StrategySpecRejected("spec is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise StrategySpecRejected("spec root must be an object")
    _exact_keys(raw, _ALLOWED_TOP, "spec")
    _scan_forbidden(raw)
    if raw.get("schema_version") != 1 or raw.get("interface_version") != 1:
        raise StrategySpecRejected("unsupported spec or interface version")
    strategy_id = raw.get("strategy_id")
    if not isinstance(strategy_id, str) or not _ID.fullmatch(strategy_id):
        raise StrategySpecRejected("strategy_id is invalid")
    name, description = raw.get("name"), raw.get("description")
    if not isinstance(name, str) or not name or len(name) > 100:
        raise StrategySpecRejected("name is invalid")
    if not isinstance(description, str) or not description or len(description) > 2000:
        raise StrategySpecRejected("description is invalid")

    data = raw.get("data")
    if not isinstance(data, dict):
        raise StrategySpecRejected("data must be an object")
    _exact_keys(data, {"event_kind", "interval", "max_age_seconds", "warmup"}, "data")
    event_kind, interval = data.get("event_kind"), data.get("interval")
    if event_kind not in {"bar", "ranking", "mark_price", "funding", "clock"}:
        raise StrategySpecRejected("event_kind is unsupported")
    if not isinstance(interval, str) or not re.fullmatch(
        r"[1-9][0-9]*[smhd]", interval
    ):
        raise StrategySpecRejected("interval is invalid")
    max_age, warmup = data.get("max_age_seconds"), data.get("warmup")
    if (
        not isinstance(max_age, int)
        or isinstance(max_age, bool)
        or not 1 <= max_age <= 86400
    ):
        raise StrategySpecRejected("max_age_seconds is invalid")
    if (
        not isinstance(warmup, int)
        or isinstance(warmup, bool)
        or not 0 <= warmup <= _MAX_WINDOW
    ):
        raise StrategySpecRejected("warmup is invalid")

    symbols_raw = raw.get("symbols")
    if not isinstance(symbols_raw, list) or not symbols_raw or len(symbols_raw) > 100:
        raise StrategySpecRejected("symbols must be a bounded non-empty list")
    if any(
        not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
        for symbol in symbols_raw
    ):
        raise StrategySpecRejected("symbol is invalid")
    if len(set(symbols_raw)) != len(symbols_raw):
        raise StrategySpecRejected("symbols must be unique")

    indicators_raw = raw.get("indicators")
    if not isinstance(indicators_raw, list) or len(indicators_raw) > _MAX_INDICATORS:
        raise StrategySpecRejected("indicators must be a bounded list")
    indicators: list[IndicatorSpec] = []
    ids: set[str] = set()
    for item in indicators_raw:
        if not isinstance(item, dict):
            raise StrategySpecRejected("indicator must be an object")
        _exact_keys(item, {"id", "kind", "field", "window"}, "indicator")
        indicator_id = item.get("id")
        kind, field, window = item.get("kind"), item.get("field"), item.get("window")
        if (
            not isinstance(indicator_id, str)
            or not _ID.fullmatch(indicator_id)
            or indicator_id in ids
        ):
            raise StrategySpecRejected("indicator id is invalid or duplicated")
        if kind not in {"sma", "ema", "min", "max"} or field not in {
            "open",
            "high",
            "low",
            "close",
            "volume",
            "mark_price",
            "funding_rate",
            "rank",
        }:
            raise StrategySpecRejected("indicator kind or field is unsupported")
        if (
            not isinstance(window, int)
            or isinstance(window, bool)
            or not 1 <= window <= _MAX_WINDOW
        ):
            raise StrategySpecRejected("indicator window is invalid")
        ids.add(indicator_id)
        indicators.append(IndicatorSpec(indicator_id, kind, field, window))

    rules_raw = raw.get("entry_rules")
    if not isinstance(rules_raw, list) or not rules_raw or len(rules_raw) > _MAX_RULES:
        raise StrategySpecRejected("entry_rules must be a bounded non-empty list")
    refs = {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "mark_price",
        "funding_rate",
        "rank",
        *ids,
    }
    rules: list[RuleSpec] = []
    rule_ids: set[str] = set()
    for item in rules_raw:
        if not isinstance(item, dict):
            raise StrategySpecRejected("rule must be an object")
        _exact_keys(item, {"id", "position_side", "reason", "when"}, "rule")
        rule_id, side, reason = (
            item.get("id"),
            item.get("position_side"),
            item.get("reason"),
        )
        if (
            not isinstance(rule_id, str)
            or not _ID.fullmatch(rule_id)
            or rule_id in rule_ids
        ):
            raise StrategySpecRejected("rule id is invalid or duplicated")
        if (
            side not in {"LONG", "SHORT"}
            or not isinstance(reason, str)
            or not reason
            or len(reason) > 300
        ):
            raise StrategySpecRejected("rule side or reason is invalid")
        expression = item.get("when")
        _validate_expr(expression, refs=refs)
        assert isinstance(expression, dict)
        rules.append(RuleSpec(rule_id, side, reason, expression))
        rule_ids.add(rule_id)

    exit_intent = raw.get("exit_intent")
    if not isinstance(exit_intent, dict):
        raise StrategySpecRejected("exit_intent must be an object")
    _exact_keys(exit_intent, {"take_profit_ratio", "stop_loss_ratio"}, "exit_intent")
    take_profit = _decimal(
        exit_intent.get("take_profit_ratio"), field="take_profit_ratio"
    )
    stop_loss = _decimal(exit_intent.get("stop_loss_ratio"), field="stop_loss_ratio")
    if not Decimal("0") < take_profit < 1 or not Decimal("0") < stop_loss < 1:
        raise StrategySpecRejected("exit ratios must be in (0, 1)")
    cooldown = raw.get("cooldown_events")
    if (
        not isinstance(cooldown, int)
        or isinstance(cooldown, bool)
        or not 0 <= cooldown <= 100000
    ):
        raise StrategySpecRejected("cooldown_events is invalid")

    scenarios_raw = raw.get("scenarios")
    if (
        not isinstance(scenarios_raw, list)
        or not scenarios_raw
        or len(scenarios_raw) > _MAX_SCENARIOS
    ):
        raise StrategySpecRejected("scenarios must be a bounded non-empty list")
    scenarios: list[ScenarioSpec] = []
    scenario_ids: set[str] = set()
    for item in scenarios_raw:
        if not isinstance(item, dict):
            raise StrategySpecRejected("scenario must be an object")
        _exact_keys(
            item,
            {"id", "category", "events", "expect_signal", "expect_error"},
            "scenario",
        )
        scenario_id, category = item.get("id"), item.get("category")
        events = item.get("events")
        if (
            not isinstance(scenario_id, str)
            or not _ID.fullmatch(scenario_id)
            or scenario_id in scenario_ids
        ):
            raise StrategySpecRejected("scenario id is invalid or duplicated")
        if category not in {
            "normal",
            "no_signal",
            "boundary",
            "data_fault",
            "abnormal",
        }:
            raise StrategySpecRejected("scenario category is invalid")
        if (
            not isinstance(events, list)
            or len(events) > 10000
            or any(not isinstance(event, dict) for event in events)
        ):
            raise StrategySpecRejected("scenario events are invalid")
        expect_signal, expect_error = (
            item.get("expect_signal"),
            item.get("expect_error"),
        )
        if (
            not isinstance(expect_signal, bool)
            or not isinstance(expect_error, bool)
            or (expect_signal and expect_error)
        ):
            raise StrategySpecRejected("scenario expectation is invalid")
        scenarios.append(
            ScenarioSpec(
                scenario_id, category, tuple(events), expect_signal, expect_error
            )
        )
        scenario_ids.add(scenario_id)

    assumptions = _string_list(raw.get("assumptions"), "assumptions")
    unresolved = _string_list(raw.get("unresolved"), "unresolved")
    assertions = _string_list(raw.get("assertions"), "assertions")
    if unresolved:
        raise StrategySpecRejected(
            "unresolved strategy questions must be resolved before evaluation"
        )

    normalized = json.loads(canonical_json(raw).decode("utf-8"))
    normalized["exit_intent"]["take_profit_ratio"] = _decimal_text(take_profit)
    normalized["exit_intent"]["stop_loss_ratio"] = _decimal_text(stop_loss)
    for rule in normalized["entry_rules"]:
        rule["when"] = _normalize_expression(rule["when"])
    for scenario in normalized["scenarios"]:
        for scenario_event in scenario["events"]:
            values = scenario_event.get("values")
            if not isinstance(values, dict):
                continue
            for key, value in tuple(values.items()):
                try:
                    parsed = _decimal(value, field=f"scenario value {key}")
                except StrategySpecRejected:
                    continue
                values[key] = _decimal_text(parsed)
    return StrategySpec(
        schema_version=1,
        interface_version=1,
        strategy_id=strategy_id,
        name=name,
        description=description,
        event_kind=event_kind,
        interval=interval,
        max_age_seconds=max_age,
        warmup=warmup,
        symbols=tuple(symbols_raw),
        indicators=tuple(indicators),
        entry_rules=tuple(rules),
        take_profit_ratio=take_profit,
        stop_loss_ratio=stop_loss,
        cooldown_events=cooldown,
        assumptions=assumptions,
        unresolved=unresolved,
        assertions=assertions,
        scenarios=tuple(scenarios),
        normalized=normalized,
    )


def migrate_strategy_spec(payload: bytes) -> bytes:
    """Migrate the only supported legacy draft without accepting unknown versions."""
    if len(payload) > _MAX_INPUT_BYTES:
        raise StrategySpecRejected("spec exceeds the input size limit")
    try:
        raw = json.loads(
            payload.decode("utf-8"),
            parse_float=_reject_float,
            parse_constant=_reject_float,
            object_pairs_hook=_object_no_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StrategySpecRejected("spec is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise StrategySpecRejected("spec root must be an object")
    version = raw.get("schema_version")
    if version == 1:
        return load_strategy_spec(canonical_json(raw)).canonical_bytes
    if version != 0 or "interface_version" in raw:
        raise StrategySpecRejected("unsupported spec migration source version")
    migrated = dict(raw)
    migrated["schema_version"] = 1
    migrated["interface_version"] = 1
    return load_strategy_spec(canonical_json(migrated)).canonical_bytes
