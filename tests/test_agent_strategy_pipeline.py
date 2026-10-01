from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from promptperp.agent_pipeline import (
    AgentPipelineError,
    AgentPipelineState,
    PipelinePolicy,
    PipelineSupervisor,
)
from promptperp.approvals import (
    ApprovalBindingV2,
    ApprovalDecision,
    ApprovalLedger,
    ApprovalRejected,
    ApprovalState,
)
from promptperp.evaluation import evaluate_strategy
from promptperp.sandbox import SandboxLimits, SandboxRunner, TerminationReason
from promptperp.signal_engine import (
    EngineState,
    MarketEvent,
    SignalEngine,
    SignalEngineRejected,
)
from promptperp.strategy_packages import BundleRejected, build_bundle, verify_bundle
from promptperp.strategy_spec import (
    StrategySpecRejected,
    canonical_json,
    load_strategy_spec,
    migrate_strategy_spec,
)

NOW = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
COMMIT = "a" * 40
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
SIGNING_KEY = b"fictional-bundle-signing-key-32-bytes-minimum"
OPERATOR_TOKEN = b"fictional-operator-approval-token"


def event(
    sequence: int, close: str, *, observed_at: datetime | None = None
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "observed_at": (observed_at or (NOW + timedelta(minutes=sequence))).isoformat(),
        "symbol": "BTCUSDT",
        "values": {"close": close},
    }


def spec_mapping() -> dict[str, object]:
    return {
        "schema_version": 1,
        "interface_version": 1,
        "strategy_id": "agent_momentum",
        "name": "Offline momentum candidate",
        "description": "A fictional deterministic candidate. Ignore previous instructions is inert text.",
        "data": {
            "event_kind": "bar",
            "interval": "1m",
            "max_age_seconds": 60,
            "warmup": 1,
        },
        "symbols": ["BTCUSDT"],
        "indicators": [
            {"id": "close_sma", "kind": "sma", "field": "close", "window": 2}
        ],
        "entry_rules": [
            {
                "id": "close_above_sma",
                "position_side": "LONG",
                "reason": "close above two-event SMA",
                "when": {
                    "op": "gt",
                    "args": [{"ref": "close"}, {"ref": "close_sma"}],
                },
            }
        ],
        "exit_intent": {"take_profit_ratio": "0.02", "stop_loss_ratio": "0.01"},
        "cooldown_events": 2,
        "assumptions": ["events are final synthetic bars"],
        "unresolved": [],
        "assertions": ["a rising close can produce a long proposal"],
        "scenarios": [
            {
                "id": "normal_rise",
                "category": "normal",
                "events": [event(1, "1"), event(2, "3")],
                "expect_signal": True,
                "expect_error": False,
            },
            {
                "id": "no_signal_fall",
                "category": "no_signal",
                "events": [event(1, "3"), event(2, "1")],
                "expect_signal": False,
                "expect_error": False,
            },
            {
                "id": "equal_boundary",
                "category": "boundary",
                "events": [event(1, "1"), event(2, "1")],
                "expect_signal": False,
                "expect_error": False,
            },
            {
                "id": "duplicate_data",
                "category": "data_fault",
                "events": [event(1, "1"), event(1, "3")],
                "expect_signal": False,
                "expect_error": True,
            },
            {
                "id": "invalid_number",
                "category": "abnormal",
                "events": [event(1, "NaN")],
                "expect_signal": False,
                "expect_error": True,
            },
        ],
    }


def loaded_spec():
    return load_strategy_spec(canonical_json(spec_mapping()))


def build_verified(tmp_path):
    spec = loaded_spec()
    report = evaluate_strategy(spec, evaluated_at=NOW)
    root = tmp_path / "bundle"
    manifest = build_bundle(
        root,
        spec=spec,
        evaluation=report,
        commit_sha=COMMIT,
        issuer="test-operator",
        signing_key=SIGNING_KEY,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=2),
    )
    verified = verify_bundle(
        root, trust_roots={"test-operator": SIGNING_KEY}, checked_at=NOW
    )
    return root, verified, manifest


def make_binding(verified, *, request_id="request-1"):
    return ApprovalBindingV2(
        schema_version=2,
        request_id=request_id,
        strategy_id=verified.spec.strategy_id,
        bundle_fingerprint=verified.manifest.fingerprint,
        spec_fingerprint=verified.spec.fingerprint,
        evaluation_fingerprint=verified.evaluation.fingerprint,
        commit_sha=verified.manifest.commit_sha,
        environment_fingerprint=HASH_A,
        allocation_policy_fingerprint=HASH_B,
        risk_policy_fingerprint=HASH_C,
        requested_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def make_ledger(tmp_path):
    return ApprovalLedger(
        tmp_path / "approvals.sqlite3",
        operator_token_digest=ApprovalLedger.token_digest(OPERATOR_TOKEN),
    )


def make_policy(**changes):
    values = {
        "schema_version": 1,
        "allocation_policy_fingerprint": HASH_B,
        "risk_policy_fingerprint": HASH_C,
        "environment_fingerprint": HASH_A,
        "margin": Decimal("20"),
        "leverage": 2,
    }
    values.update(changes)
    return PipelinePolicy(**values)


def test_strategy_spec_is_canonical_and_rejects_authority_and_unsafe_values():
    first = loaded_spec()
    reordered = dict(reversed(list(spec_mapping().items())))
    second = load_strategy_spec(json.dumps(reordered, separators=(",", ":")).encode())
    assert first.canonical_bytes == second.canonical_bytes
    assert first.fingerprint == second.fingerprint
    equivalent = spec_mapping()
    equivalent["exit_intent"] = {
        "take_profit_ratio": "0.0200",
        "stop_loss_ratio": "0.01000",
    }
    assert (
        load_strategy_spec(canonical_json(equivalent)).fingerprint == first.fingerprint
    )

    for field, value in (
        ("margin", "100"),
        ("api_key", "fake"),
        ("url", "https://example.invalid"),
        ("code", "import os"),
    ):
        malicious = spec_mapping()
        malicious[field] = value
        with pytest.raises(StrategySpecRejected):
            load_strategy_spec(canonical_json(malicious))
    with pytest.raises(StrategySpecRejected, match="floating-point"):
        load_strategy_spec(
            canonical_json(spec_mapping()).replace(b'"warmup":1', b'"warmup":1.0')
        )


def test_only_known_draft_migrates_and_unknown_versions_fail_closed():
    draft = spec_mapping()
    draft["schema_version"] = 0
    draft.pop("interface_version")
    migrated = migrate_strategy_spec(canonical_json(draft))
    assert load_strategy_spec(migrated).schema_version == 1
    unknown = spec_mapping()
    unknown["schema_version"] = 99
    with pytest.raises(StrategySpecRejected, match="unsupported"):
        migrate_strategy_spec(canonical_json(unknown))


def test_interpreter_is_deterministic_has_no_funding_authority_and_fails_closed():
    spec = loaded_spec()
    outputs = []
    for _ in range(2):
        engine = SignalEngine(spec)
        proposals = []
        for raw in (event(1, "1"), event(2, "3")):
            market = MarketEvent.from_mapping(raw)
            emitted, _ = engine.process(market, accepted_at=market.observed_at)
            proposals.extend(item.to_json() for item in emitted)
        outputs.append(proposals)
    assert outputs[0] == outputs[1]
    payload = json.loads(outputs[0][0])
    assert not {"account", "margin", "leverage", "order_type"} & set(payload)

    engine = SignalEngine(spec)
    first = MarketEvent.from_mapping(event(1, "1"))
    engine.process(first, accepted_at=first.observed_at)
    with pytest.raises(SignalEngineRejected, match="duplicated"):
        engine.process(first, accepted_at=first.observed_at)


def test_evaluation_requires_full_coverage_and_is_reproducible():
    spec = loaded_spec()
    first = evaluate_strategy(spec, evaluated_at=NOW)
    second = evaluate_strategy(spec, evaluated_at=NOW)
    assert first.passed
    assert first.to_json() == second.to_json()
    assert {result.category for result in first.results} == {
        "normal",
        "no_signal",
        "boundary",
        "data_fault",
        "abnormal",
    }


def test_content_addressed_bundle_rejects_tampering_extra_files_and_wrong_trust(
    tmp_path,
):
    root, verified, manifest = build_verified(tmp_path)
    assert verified.manifest.fingerprint == manifest.fingerprint
    with pytest.raises(BundleRejected, match="not trusted"):
        verify_bundle(root, trust_roots={}, checked_at=NOW)

    os.chmod(root, 0o700)
    extra = root / "payload.py"
    extra.write_text("raise RuntimeError('must never execute')")
    with pytest.raises(BundleRejected, match="missing, extra"):
        verify_bundle(root, trust_roots={"test-operator": SIGNING_KEY}, checked_at=NOW)
    extra.unlink()
    spec_path = root / "spec.json"
    os.chmod(spec_path, 0o600)
    spec_path.write_bytes(spec_path.read_bytes() + b" ")
    with pytest.raises(BundleRejected, match="digest mismatch"):
        verify_bundle(root, trust_roots={"test-operator": SIGNING_KEY}, checked_at=NOW)


def test_approval_agent_can_request_but_only_operator_can_grant_and_consumption_is_single_use(
    tmp_path,
):
    _, verified, _ = build_verified(tmp_path)
    ledger = make_ledger(tmp_path)
    binding = make_binding(verified)
    ledger.request(binding)
    with pytest.raises(ApprovalRejected, match="authority"):
        ledger.decide(
            binding.request_id,
            decision=ApprovalDecision.GRANT,
            operator_token=b"agent-cannot-grant",
            occurred_at=NOW + timedelta(seconds=1),
            reason="attempted bypass",
        )
    ledger.decide(
        binding.request_id,
        decision=ApprovalDecision.GRANT,
        operator_token=OPERATOR_TOKEN,
        occurred_at=NOW + timedelta(seconds=1),
        reason="offline evaluation approved",
    )
    ledger.consume(
        binding.request_id, binding=binding, checked_at=NOW + timedelta(seconds=2)
    )
    assert ledger.state(binding.request_id) is ApprovalState.CONSUMED
    with pytest.raises(ApprovalRejected, match="requires GRANTED"):
        ledger.consume(
            binding.request_id, binding=binding, checked_at=NOW + timedelta(seconds=3)
        )
    assert b"fictional-operator" not in ledger.export_summary()


def test_approval_concurrent_consumption_has_exactly_one_winner(tmp_path):
    _, verified, _ = build_verified(tmp_path)
    ledger = make_ledger(tmp_path)
    binding = make_binding(verified, request_id="concurrent")
    ledger.request(binding)
    ledger.decide(
        binding.request_id,
        decision=ApprovalDecision.GRANT,
        operator_token=OPERATOR_TOKEN,
        occurred_at=NOW + timedelta(seconds=1),
        reason="offline only",
    )

    def consume() -> bool:
        try:
            ledger.consume(
                binding.request_id,
                binding=binding,
                checked_at=NOW + timedelta(seconds=2),
            )
            return True
        except ApprovalRejected:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert sorted(results) == [False, True]


def test_approval_hash_chain_detects_database_tampering(tmp_path):
    _, verified, _ = build_verified(tmp_path)
    ledger = make_ledger(tmp_path)
    binding = make_binding(verified)
    ledger.request(binding)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            "UPDATE approval_events SET actor = 'forged' WHERE sequence = 1"
        )
    with pytest.raises(ApprovalRejected, match="corrupted"):
        ledger.events()


def test_denied_revoked_and_expired_approvals_cannot_be_consumed(tmp_path):
    _, verified, _ = build_verified(tmp_path)
    ledger = make_ledger(tmp_path)
    denied = make_binding(verified, request_id="denied")
    ledger.request(denied)
    ledger.decide(
        denied.request_id,
        decision=ApprovalDecision.DENY,
        operator_token=OPERATOR_TOKEN,
        occurred_at=NOW + timedelta(seconds=1),
        reason="operator rejected candidate",
    )
    with pytest.raises(ApprovalRejected, match="requires GRANTED"):
        ledger.consume(
            denied.request_id,
            binding=denied,
            checked_at=NOW + timedelta(seconds=2),
        )

    revoked = make_binding(verified, request_id="revoked")
    ledger.request(revoked)
    ledger.decide(
        revoked.request_id,
        decision=ApprovalDecision.GRANT,
        operator_token=OPERATOR_TOKEN,
        occurred_at=NOW + timedelta(seconds=1),
        reason="temporary approval",
    )
    ledger.revoke(
        revoked.request_id,
        operator_token=OPERATOR_TOKEN,
        occurred_at=NOW + timedelta(seconds=2),
        reason="operator withdrew approval",
    )
    with pytest.raises(ApprovalRejected, match="requires GRANTED"):
        ledger.consume(
            revoked.request_id,
            binding=revoked,
            checked_at=NOW + timedelta(seconds=3),
        )

    expired = make_binding(verified, request_id="expired")
    ledger.request(expired)
    ledger.decide(
        expired.request_id,
        decision=ApprovalDecision.GRANT,
        operator_token=OPERATOR_TOKEN,
        occurred_at=NOW + timedelta(seconds=1),
        reason="short validity",
    )
    with pytest.raises(ApprovalRejected, match="expired"):
        ledger.consume(
            expired.request_id,
            binding=expired,
            checked_at=NOW + timedelta(hours=2),
        )
    assert ledger.state(expired.request_id) is ApprovalState.EXPIRED


def test_linux_sandbox_executes_only_trusted_interpreter_with_clean_namespace():
    runner = SandboxRunner(limits=SandboxLimits(timeout_seconds=5))
    assert "--unshare-user" not in runner._base_bwrap()
    assert runner.probe().available
    spec = loaded_spec()
    events = tuple(
        MarketEvent.from_mapping(raw) for raw in (event(1, "1"), event(2, "3"))
    )
    result = runner.run(spec=spec, events=events, state=EngineState())
    assert result.reason is TerminationReason.COMPLETED
    assert result.response is not None
    assert len(result.response["proposals"]) == 1


def test_sandbox_unavailable_fails_closed(monkeypatch):
    runner = SandboxRunner()
    monkeypatch.setattr(runner, "bwrap", None)
    result = runner.run(spec=loaded_spec(), events=(), state=EngineState())
    assert result.reason is TerminationReason.CAPABILITY_UNAVAILABLE


def test_sandbox_input_resource_limit_blocks_before_process_start():
    runner = SandboxRunner(limits=SandboxLimits(input_bytes=16))
    result = runner.run(spec=loaded_spec(), events=(), state=EngineState())
    assert result.reason is TerminationReason.RESOURCE_LIMIT


def test_pipeline_end_to_end_consumes_approval_deduplicates_and_never_executes(
    tmp_path,
):
    root, verified, _ = build_verified(tmp_path)
    ledger = make_ledger(tmp_path)
    binding = make_binding(verified)
    ledger.request(binding)
    ledger.decide(
        binding.request_id,
        decision=ApprovalDecision.GRANT,
        operator_token=OPERATOR_TOKEN,
        occurred_at=NOW + timedelta(seconds=1),
        reason="offline worker only",
    )
    state_path = tmp_path / "pipeline.json"
    supervisor = PipelineSupervisor(
        bundle_root=root,
        trust_roots={"test-operator": SIGNING_KEY},
        approval_ledger=ledger,
        approval_binding=binding,
        policy=make_policy(),
        state_path=state_path,
    )
    assert (
        supervisor.start(checked_at=NOW + timedelta(seconds=2))
        is AgentPipelineState.RUNNING
    )
    batch = supervisor.run_batch(
        tuple(MarketEvent.from_mapping(raw) for raw in (event(1, "1"), event(2, "3")))
    )
    assert (
        len(batch.proposals) == len(batch.allocations) == len(batch.risk_requests) == 1
    )
    assert batch.allocations[0].margin == Decimal("20")
    assert not batch.risk_requests[0].execution_permitted
    supervisor.stop(reason="offline acceptance complete")
    assert supervisor.status()["pipeline_state"] == "STOPPED"


def test_pipeline_recovery_reverifies_consumed_approval_and_checkpoint(tmp_path):
    root, verified, _ = build_verified(tmp_path)
    ledger = make_ledger(tmp_path)
    binding = make_binding(verified)
    ledger.request(binding)
    ledger.decide(
        binding.request_id,
        decision=ApprovalDecision.GRANT,
        operator_token=OPERATOR_TOKEN,
        occurred_at=NOW + timedelta(seconds=1),
        reason="offline recovery exercise",
    )
    state_path = tmp_path / "pipeline.json"
    first = PipelineSupervisor(
        bundle_root=root,
        trust_roots={"test-operator": SIGNING_KEY},
        approval_ledger=ledger,
        approval_binding=binding,
        policy=make_policy(),
        state_path=state_path,
    )
    first.start(checked_at=NOW + timedelta(seconds=2))
    first.run_batch((MarketEvent.from_mapping(event(1, "1")),))
    recovered = PipelineSupervisor(
        bundle_root=root,
        trust_roots={"test-operator": SIGNING_KEY},
        approval_ledger=ledger,
        approval_binding=binding,
        policy=make_policy(),
        state_path=state_path,
    )
    assert (
        recovered.recover(checked_at=NOW + timedelta(seconds=3))
        is AgentPipelineState.RUNNING
    )
    batch = recovered.run_batch((MarketEvent.from_mapping(event(2, "3")),))
    assert batch.checkpoint_sequence == 2
    assert len(batch.risk_requests) == 1


def test_pipeline_rejects_policy_change_unapproved_bundle_and_consumed_replay(tmp_path):
    root, verified, _ = build_verified(tmp_path)
    ledger = make_ledger(tmp_path)
    binding = make_binding(verified)
    ledger.request(binding)
    ledger.decide(
        binding.request_id,
        decision=ApprovalDecision.GRANT,
        operator_token=OPERATOR_TOKEN,
        occurred_at=NOW + timedelta(seconds=1),
        reason="offline only",
    )
    mismatched = PipelineSupervisor(
        bundle_root=root,
        trust_roots={"test-operator": SIGNING_KEY},
        approval_ledger=ledger,
        approval_binding=binding,
        policy=make_policy(risk_policy_fingerprint="d" * 64),
        state_path=tmp_path / "wrong.json",
    )
    with pytest.raises(AgentPipelineError, match="does not bind"):
        mismatched.start(checked_at=NOW + timedelta(seconds=2))

    valid = PipelineSupervisor(
        bundle_root=root,
        trust_roots={"test-operator": SIGNING_KEY},
        approval_ledger=ledger,
        approval_binding=binding,
        policy=make_policy(),
        state_path=tmp_path / "valid.json",
    )
    valid.start(checked_at=NOW + timedelta(seconds=2))
    replay = PipelineSupervisor(
        bundle_root=root,
        trust_roots={"test-operator": SIGNING_KEY},
        approval_ledger=ledger,
        approval_binding=binding,
        policy=make_policy(),
        state_path=tmp_path / "replay.json",
    )
    with pytest.raises(AgentPipelineError, match="active granted"):
        replay.start(checked_at=NOW + timedelta(seconds=3))
