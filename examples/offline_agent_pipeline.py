from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from promptperp.agent_pipeline import PipelinePolicy, PipelineSupervisor
from promptperp.approvals import ApprovalBindingV2, ApprovalDecision, ApprovalLedger
from promptperp.evaluation import evaluate_strategy
from promptperp.signal_engine import MarketEvent
from promptperp.strategy_packages import build_bundle, verify_bundle
from promptperp.strategy_spec import load_strategy_spec

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def main() -> None:
    spec = load_strategy_spec(
        (ROOT / "examples/agent_strategy_spec.example.json").read_bytes()
    )
    evaluation = evaluate_strategy(spec, evaluated_at=NOW)
    signing_key = b"x" * 32
    operator_token = b"y" * 32
    with tempfile.TemporaryDirectory(prefix="promptperp-agent-example-") as temporary:
        work = Path(temporary)
        bundle_root = work / "bundle"
        build_bundle(
            bundle_root,
            spec=spec,
            evaluation=evaluation,
            commit_sha="a" * 40,
            issuer="offline-example",
            signing_key=signing_key,
            issued_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
        )
        verified = verify_bundle(
            bundle_root,
            trust_roots={"offline-example": signing_key},
            checked_at=NOW,
        )
        ledger = ApprovalLedger(
            work / "approvals.sqlite3",
            operator_token_digest=ApprovalLedger.token_digest(operator_token),
        )
        binding = ApprovalBindingV2(
            schema_version=2,
            request_id="offline-example-request",
            strategy_id=spec.strategy_id,
            bundle_fingerprint=verified.manifest.fingerprint,
            spec_fingerprint=spec.fingerprint,
            evaluation_fingerprint=evaluation.fingerprint,
            commit_sha="a" * 40,
            environment_fingerprint="b" * 64,
            allocation_policy_fingerprint="c" * 64,
            risk_policy_fingerprint="d" * 64,
            requested_at=NOW,
            expires_at=NOW + timedelta(minutes=30),
        )
        ledger.request(binding)
        ledger.decide(
            binding.request_id,
            decision=ApprovalDecision.GRANT,
            operator_token=operator_token,
            occurred_at=NOW + timedelta(seconds=1),
            reason="fictional offline demonstration",
        )
        supervisor = PipelineSupervisor(
            bundle_root=bundle_root,
            trust_roots={"offline-example": signing_key},
            approval_ledger=ledger,
            approval_binding=binding,
            policy=PipelinePolicy(
                schema_version=1,
                allocation_policy_fingerprint="c" * 64,
                risk_policy_fingerprint="d" * 64,
                environment_fingerprint="b" * 64,
                margin=Decimal("20"),
                leverage=2,
            ),
            state_path=work / "pipeline.json",
        )
        supervisor.start(checked_at=NOW + timedelta(seconds=2))
        scenario = spec.scenarios[0]
        result = supervisor.run_batch(
            tuple(MarketEvent.from_mapping(event) for event in scenario.events)
        )
        supervisor.stop(reason="offline demonstration complete")
        print(
            json.dumps(
                {
                    "mode": "offline",
                    "evaluation": "PASSED" if evaluation.passed else "FAILED",
                    "proposals": len(result.proposals),
                    "execution_permitted": any(
                        request.execution_permitted for request in result.risk_requests
                    ),
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
