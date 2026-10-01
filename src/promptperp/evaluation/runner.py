from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from promptperp.signal_engine import (
    MarketEvent,
    SignalEngine,
    SignalEngineRejected,
    SignalProposal,
)
from promptperp.strategy_spec import StrategySpec, canonical_json

_REQUIRED_CATEGORIES = {"normal", "no_signal", "boundary", "data_fault", "abnormal"}
_INTERPRETER_ID = "promptperp-declarative-v1"


class EvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    category: str
    passed: bool
    proposals: int
    error_code: str | None
    output_fingerprint: str


@dataclass(frozen=True)
class EvaluationReport:
    schema_version: int
    strategy_id: str
    spec_fingerprint: str
    interpreter_id: str
    fixtures_fingerprint: str
    evaluated_at: datetime
    passed: bool
    reason_codes: tuple[str, ...]
    results: tuple[ScenarioResult, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.evaluated_at.tzinfo is None:
            raise ValueError("evaluation report schema or time is invalid")
        if self.passed != (self.reason_codes == ("PASSED",)):
            raise ValueError("evaluation result and reasons disagree")

    @property
    def fingerprint(self) -> str:
        return sha256(self.to_json()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "strategy_id": self.strategy_id,
            "spec_fingerprint": self.spec_fingerprint,
            "interpreter_id": self.interpreter_id,
            "fixtures_fingerprint": self.fixtures_fingerprint,
            "evaluated_at": self.evaluated_at.isoformat(),
            "passed": self.passed,
            "reason_codes": list(self.reason_codes),
            "results": [asdict(result) for result in self.results],
        }

    def to_json(self) -> bytes:
        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, payload: bytes) -> EvaluationReport:
        try:
            raw = json.loads(payload)
            results = tuple(ScenarioResult(**item) for item in raw["results"])
            return cls(
                schema_version=raw["schema_version"],
                strategy_id=raw["strategy_id"],
                spec_fingerprint=raw["spec_fingerprint"],
                interpreter_id=raw["interpreter_id"],
                fixtures_fingerprint=raw["fixtures_fingerprint"],
                evaluated_at=datetime.fromisoformat(raw["evaluated_at"]),
                passed=raw["passed"],
                reason_codes=tuple(raw["reason_codes"]),
                results=results,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EvaluationError("evaluation report is malformed") from exc


def _run_scenario(spec: StrategySpec, scenario_index: int) -> ScenarioResult:
    scenario = spec.scenarios[scenario_index]
    engine = SignalEngine(spec)
    proposals: list[SignalProposal] = []
    error: str | None = None
    try:
        for raw in scenario.events:
            event = MarketEvent.from_mapping(raw)
            emitted, _ = engine.process(event, accepted_at=event.observed_at)
            proposals.extend(emitted)
    except (SignalEngineRejected, ValueError, ArithmeticError):
        error = "INPUT_REJECTED"
    observed_signal = bool(proposals)
    passed = (
        error is not None
        if scenario.expect_error
        else (error is None and observed_signal == scenario.expect_signal)
    )
    output = canonical_json(
        {
            "error": error,
            "proposals": [proposal.to_dict() for proposal in proposals],
            "state": engine.state.to_dict(),
        }
    )
    return ScenarioResult(
        scenario_id=scenario.scenario_id,
        category=scenario.category,
        passed=passed,
        proposals=len(proposals),
        error_code=error,
        output_fingerprint=sha256(output).hexdigest(),
    )


def evaluate_strategy(
    spec: StrategySpec, *, evaluated_at: datetime
) -> EvaluationReport:
    if evaluated_at.tzinfo is None:
        raise ValueError("evaluation time must be timezone-aware")
    first = tuple(_run_scenario(spec, index) for index in range(len(spec.scenarios)))
    second = tuple(_run_scenario(spec, index) for index in range(len(spec.scenarios)))
    reasons: list[str] = []
    categories = {scenario.category for scenario in spec.scenarios}
    if not _REQUIRED_CATEGORIES <= categories:
        reasons.append("SCENARIO_COVERAGE_INCOMPLETE")
    if first != second:
        reasons.append("NON_DETERMINISTIC_OUTPUT")
    if any(not result.passed for result in first):
        reasons.append("SCENARIO_FAILED")
    fixtures = canonical_json(
        [
            {
                "id": scenario.scenario_id,
                "category": scenario.category,
                "events": [dict(event) for event in scenario.events],
                "expect_signal": scenario.expect_signal,
                "expect_error": scenario.expect_error,
            }
            for scenario in spec.scenarios
        ]
    )
    return EvaluationReport(
        schema_version=1,
        strategy_id=spec.strategy_id,
        spec_fingerprint=spec.fingerprint,
        interpreter_id=_INTERPRETER_ID,
        fixtures_fingerprint=sha256(fixtures).hexdigest(),
        evaluated_at=evaluated_at,
        passed=not reasons,
        reason_codes=tuple(reasons or ["PASSED"]),
        results=first,
    )
