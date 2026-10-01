from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from promptperp.strategy_packages import (
    ApprovalBinding,
    StrategyPackageRejected,
    validate_source,
    verify_approval,
)

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
PARAMS = "b" * 64
ENVIRONMENT = "c" * 64
RISK_POLICY = "d" * 64
SAFE_SOURCE = b"""from decimal import Decimal
from promptperp.strategies.base import StrategySignal

def build_margin():
    return Decimal("200")
"""


def approval_for(manifest):
    return ApprovalBinding(
        schema_version=1,
        approval_id="approval-a8",
        strategy_id=manifest.strategy_id,
        strategy_fingerprint=manifest.fingerprint,
        parameter_fingerprint=manifest.parameter_fingerprint,
        commit_sha="a" * 40,
        environment_fingerprint=ENVIRONMENT,
        risk_policy_fingerprint=RISK_POLICY,
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


def test_source_validation_is_deterministic_and_does_not_execute_code():
    source = SAFE_SOURCE + b'\ndef never_execute():\n    raise RuntimeError("never")\n'

    first = validate_source(
        strategy_id="generated_alpha",
        source=source,
        parameter_fingerprint=PARAMS,
    )
    second = validate_source(
        strategy_id="generated_alpha",
        source=source,
        parameter_fingerprint=PARAMS,
    )

    assert first == second
    assert first.fingerprint == second.fingerprint


@pytest.mark.parametrize(
    "source",
    [
        b"import os\nvalue = os.environ\n",
        b"from promptperp.exchange import BinanceFuturesAdapter\n",
        b"from promptperp.execution import ExecutionCoordinator\n",
        b"from promptperp.risk import RiskEngine\n",
        b"open('/tmp/exfiltrate', 'w')\n",
        b"__import__('os').environ\n",
        b"object.__subclasses__()\n",
        b"global stolen\nstolen = 'value'\n",
        b"import typing\nvalue = typing.sys.modules\n",
        b"getattr(object, '__subclasses__')()\n",
        b"raise RuntimeError('top-level')\n",
    ],
)
def test_malicious_capabilities_are_rejected_without_importing(source):
    with pytest.raises(StrategyPackageRejected, match="forbidden"):
        validate_source(
            strategy_id="malicious",
            source=source,
            parameter_fingerprint=PARAMS,
        )


def test_approval_binds_source_parameters_commit_environment_and_risk_policy():
    manifest = validate_source(
        strategy_id="generated_alpha",
        source=SAFE_SOURCE,
        parameter_fingerprint=PARAMS,
    )
    approval = approval_for(manifest)

    accepted = verify_approval(
        manifest=manifest,
        approval=approval,
        commit_sha="a" * 40,
        environment_fingerprint=ENVIRONMENT,
        risk_policy_fingerprint=RISK_POLICY,
        checked_at=NOW,
    )
    assert accepted.accepted
    assert accepted.reason_codes == ("ACCEPTED",)
    assert json.loads(accepted.to_json())["accepted"] is True

    changed_source = validate_source(
        strategy_id="generated_alpha",
        source=SAFE_SOURCE + b"\n# changed\n",
        parameter_fingerprint="e" * 64,
    )
    rejected = verify_approval(
        manifest=changed_source,
        approval=approval,
        commit_sha="b" * 40,
        environment_fingerprint="f" * 64,
        risk_policy_fingerprint="0" * 64,
        checked_at=NOW + timedelta(hours=1),
    )

    assert not rejected.accepted
    assert set(rejected.reason_codes) == {
        "APPROVAL_EXPIRED",
        "STRATEGY_VERSION_MISMATCH",
        "PARAMETER_MISMATCH",
        "COMMIT_MISMATCH",
        "ENVIRONMENT_MISMATCH",
        "RISK_POLICY_MISMATCH",
    }


def test_invalid_source_and_naive_timestamps_fail_closed():
    with pytest.raises(StrategyPackageRejected):
        validate_source(
            strategy_id="broken",
            source=b"def broken(:",
            parameter_fingerprint=PARAMS,
        )
    manifest = validate_source(
        strategy_id="safe",
        source=SAFE_SOURCE,
        parameter_fingerprint=PARAMS,
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        verify_approval(
            manifest=manifest,
            approval=approval_for(manifest),
            commit_sha="a" * 40,
            environment_fingerprint=ENVIRONMENT,
            risk_policy_fingerprint=RISK_POLICY,
            checked_at=datetime(2026, 9, 1),
        )
