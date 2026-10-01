from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from promptperp.approvals import ApprovalBindingV2, ApprovalLedger, ApprovalState
from promptperp.sandbox import SandboxRunner, TerminationReason
from promptperp.signal_engine import EngineState, MarketEvent
from promptperp.strategy_packages import VerifiedBundle, verify_bundle
from promptperp.strategy_spec import canonical_json


class AgentPipelineError(RuntimeError):
    pass


class AgentPipelineState(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class PipelinePolicy:
    schema_version: int
    allocation_policy_fingerprint: str
    risk_policy_fingerprint: str
    environment_fingerprint: str
    margin: Decimal
    leverage: int
    maximum_proposals_per_batch: int = 8

    def __post_init__(self) -> None:
        fingerprints = (
            self.allocation_policy_fingerprint,
            self.risk_policy_fingerprint,
            self.environment_fingerprint,
        )
        if self.schema_version != 1 or any(len(value) != 64 for value in fingerprints):
            raise ValueError("pipeline policy identity is invalid")
        if not self.margin.is_finite() or self.margin <= 0 or self.leverage <= 0:
            raise ValueError("pipeline allocation is invalid")
        if not 1 <= self.maximum_proposals_per_batch <= 100:
            raise ValueError("pipeline proposal limit is invalid")


@dataclass(frozen=True)
class OfflineAllocationDecision:
    strategy_id: str
    deduplication_key: str
    margin: Decimal
    leverage: int
    policy_fingerprint: str


@dataclass(frozen=True)
class OfflineRiskRequest:
    strategy_id: str
    symbol: str
    position_side: str
    deduplication_key: str
    margin: Decimal
    leverage: int
    take_profit_ratio: Decimal
    stop_loss_ratio: Decimal
    risk_policy_fingerprint: str
    execution_permitted: bool = False

    def __post_init__(self) -> None:
        if self.execution_permitted:
            raise ValueError("agent pipeline can never permit execution")


@dataclass(frozen=True)
class BatchResult:
    proposals: tuple[Mapping[str, Any], ...]
    allocations: tuple[OfflineAllocationDecision, ...]
    risk_requests: tuple[OfflineRiskRequest, ...]
    checkpoint_sequence: int


class PipelineSupervisor:
    def __init__(
        self,
        *,
        bundle_root: str | Path,
        trust_roots: Mapping[str, bytes],
        approval_ledger: ApprovalLedger,
        approval_binding: ApprovalBindingV2,
        policy: PipelinePolicy,
        state_path: str | Path,
        sandbox: SandboxRunner | None = None,
    ):
        self.bundle_root = Path(bundle_root)
        self.trust_roots = dict(trust_roots)
        self.ledger = approval_ledger
        self.binding = approval_binding
        self.policy = policy
        self.state_path = Path(state_path)
        self.sandbox = sandbox or SandboxRunner()
        self._bundle: VerifiedBundle | None = None

    def start(self, *, checked_at: datetime) -> AgentPipelineState:
        if checked_at.tzinfo is None:
            raise ValueError("pipeline start time must be timezone-aware")
        if self.state_path.exists():
            state = self._read_runtime()["pipeline_state"]
            if state != AgentPipelineState.STOPPED.value:
                raise AgentPipelineError(
                    "pipeline state already exists and is not stopped"
                )
        capability = self.sandbox.probe()
        if not capability.available:
            raise AgentPipelineError("required sandbox isolation is unavailable")
        bundle = verify_bundle(
            self.bundle_root, trust_roots=self.trust_roots, checked_at=checked_at
        )
        self._verify_binding(bundle)
        if (
            self.ledger.state(self.binding.request_id, checked_at=checked_at)
            is not ApprovalState.GRANTED
        ):
            raise AgentPipelineError("pipeline requires an active granted approval")
        self.ledger.consume(
            self.binding.request_id, binding=self.binding, checked_at=checked_at
        )
        self._bundle = bundle
        self._write_runtime(
            {
                "schema_version": 1,
                "pipeline_state": AgentPipelineState.RUNNING.value,
                "bundle_fingerprint": bundle.manifest.fingerprint,
                "approval_request_id": self.binding.request_id,
                "engine_state": EngineState().to_dict(),
                "emitted_keys": [],
                "blocking_reason": None,
            }
        )
        return AgentPipelineState.RUNNING

    def run_batch(self, events: tuple[MarketEvent, ...]) -> BatchResult:
        runtime = self._read_runtime()
        if runtime["pipeline_state"] != AgentPipelineState.RUNNING.value:
            raise AgentPipelineError("pipeline is not running")
        bundle = self._bundle
        if bundle is None:
            raise AgentPipelineError("pipeline bundle must be reverified after restart")
        state_raw = runtime["engine_state"]
        if not isinstance(state_raw, dict):
            return self._block("CHECKPOINT_CORRUPTED")
        try:
            state = EngineState.from_mapping(state_raw)
        except Exception:
            return self._block("CHECKPOINT_CORRUPTED")
        result = self.sandbox.run(spec=bundle.spec, events=events, state=state)
        if result.reason is not TerminationReason.COMPLETED or result.response is None:
            return self._block(f"SANDBOX_{result.reason.value}")
        response = result.response
        proposals_raw = response.get("proposals")
        next_state_raw = response.get("state")
        if not isinstance(proposals_raw, list) or not isinstance(next_state_raw, dict):
            return self._block("WORKER_PROTOCOL_INVALID")
        if len(proposals_raw) > self.policy.maximum_proposals_per_batch:
            return self._block("PROPOSAL_LIMIT_EXCEEDED")
        emitted = set(runtime.get("emitted_keys", []))
        proposals: list[Mapping[str, Any]] = []
        allocations: list[OfflineAllocationDecision] = []
        risks: list[OfflineRiskRequest] = []
        forbidden = {
            "account",
            "margin",
            "leverage",
            "order_type",
            "execution_permitted",
        }
        for raw in proposals_raw:
            if not isinstance(raw, dict) or forbidden & set(raw):
                return self._block("PROPOSAL_AUTHORITY_VIOLATION")
            key = raw.get("deduplication_key")
            if not isinstance(key, str) or len(key) != 64:
                return self._block("PROPOSAL_IDENTITY_INVALID")
            if key in emitted:
                continue
            try:
                take_profit = Decimal(raw["take_profit_ratio"])
                stop_loss = Decimal(raw["stop_loss_ratio"])
                strategy_id = str(raw["strategy_id"])
                symbol = str(raw["symbol"])
                position_side = str(raw["position_side"])
            except (KeyError, InvalidOperation):
                return self._block("PROPOSAL_ENCODING_INVALID")
            if strategy_id != bundle.spec.strategy_id:
                return self._block("PROPOSAL_STRATEGY_MISMATCH")
            allocation = OfflineAllocationDecision(
                strategy_id=strategy_id,
                deduplication_key=key,
                margin=self.policy.margin,
                leverage=self.policy.leverage,
                policy_fingerprint=self.policy.allocation_policy_fingerprint,
            )
            risk = OfflineRiskRequest(
                strategy_id=strategy_id,
                symbol=symbol,
                position_side=position_side,
                deduplication_key=key,
                margin=allocation.margin,
                leverage=allocation.leverage,
                take_profit_ratio=take_profit,
                stop_loss_ratio=stop_loss,
                risk_policy_fingerprint=self.policy.risk_policy_fingerprint,
            )
            emitted.add(key)
            proposals.append(raw)
            allocations.append(allocation)
            risks.append(risk)
        try:
            next_state = EngineState.from_mapping(next_state_raw)
        except Exception:
            return self._block("WORKER_CHECKPOINT_INVALID")
        runtime["engine_state"] = next_state.to_dict()
        runtime["emitted_keys"] = sorted(emitted)
        self._write_runtime(runtime)
        return BatchResult(
            tuple(proposals), tuple(allocations), tuple(risks), next_state.last_sequence
        )

    def stop(self, *, reason: str) -> AgentPipelineState:
        if not reason:
            raise ValueError("pipeline stop reason is required")
        runtime = self._read_runtime()
        runtime["pipeline_state"] = AgentPipelineState.STOPPED.value
        runtime["blocking_reason"] = reason
        self._write_runtime(runtime)
        return AgentPipelineState.STOPPED

    def status(self) -> Mapping[str, Any]:
        return self._read_runtime()

    def recover(self, *, checked_at: datetime) -> AgentPipelineState:
        runtime = self._read_runtime()
        if runtime["pipeline_state"] == AgentPipelineState.STOPPED.value:
            raise AgentPipelineError(
                "stopped pipeline cannot be resumed with a consumed approval"
            )
        bundle = verify_bundle(
            self.bundle_root, trust_roots=self.trust_roots, checked_at=checked_at
        )
        self._verify_binding(bundle)
        if self.ledger.state(self.binding.request_id) is not ApprovalState.CONSUMED:
            raise AgentPipelineError("recovery requires the original consumed approval")
        if runtime.get("bundle_fingerprint") != bundle.manifest.fingerprint:
            raise AgentPipelineError("recovery bundle does not match checkpoint")
        self._bundle = bundle
        return AgentPipelineState(runtime["pipeline_state"])

    def _verify_binding(self, bundle: VerifiedBundle) -> None:
        expected = {
            "strategy_id": bundle.spec.strategy_id,
            "bundle_fingerprint": bundle.manifest.fingerprint,
            "spec_fingerprint": bundle.spec.fingerprint,
            "evaluation_fingerprint": bundle.evaluation.fingerprint,
            "commit_sha": bundle.manifest.commit_sha,
            "environment_fingerprint": self.policy.environment_fingerprint,
            "allocation_policy_fingerprint": self.policy.allocation_policy_fingerprint,
            "risk_policy_fingerprint": self.policy.risk_policy_fingerprint,
        }
        actual = {key: getattr(self.binding, key) for key in expected}
        if actual != expected:
            raise AgentPipelineError(
                "approval does not bind the current bundle and policies"
            )

    def _block(self, reason: str) -> BatchResult:
        runtime = self._read_runtime()
        runtime["pipeline_state"] = AgentPipelineState.BLOCKED.value
        runtime["blocking_reason"] = reason
        self._write_runtime(runtime)
        return BatchResult(
            (), (), (), int(runtime.get("engine_state", {}).get("last_sequence", 0))
        )

    def _read_runtime(self) -> dict[str, Any]:
        try:
            payload = self.state_path.read_bytes()
            raw = json.loads(payload)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AgentPipelineError("pipeline state is missing or corrupted") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise AgentPipelineError("pipeline state is untrusted")
        return raw

    def _write_runtime(self, value: Mapping[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = canonical_json(value)
        descriptor, name = tempfile.mkstemp(
            prefix=".pipeline-", dir=self.state_path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(name, self.state_path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if os.path.exists(name):
                os.unlink(name)
