from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from promptperp.config import (
    ExternalEffectBlocked,
    LiveRunApproval,
    RuntimeConfig,
    RuntimeConfigurationError,
    RuntimeMode,
)


def make_approval(**overrides):
    now = datetime.now(timezone.utc)
    values = {
        "strategy_id": "strategy-a",
        "run_id": "run-1",
        "commit_sha": "abc123",
        "approved_at": now - timedelta(minutes=1),
        "expires_at": now + timedelta(minutes=10),
        "max_margin": 400.0,
        "max_notional": 2_000.0,
        "max_leverage": 5,
        "allowed_symbols": frozenset({"BTCUSDT"}),
        "allowed_sides": frozenset({"BUY", "SELL"}),
    }
    values.update(overrides)
    return LiveRunApproval(**values)


def make_live_config(approval=None, **overrides):
    values = {
        "mode": RuntimeMode.LIVE,
        "external_effects_enabled": True,
        "strategy_id": "strategy-a",
        "run_id": "run-1",
        "commit_sha": "abc123",
        "api_key": "test-key",
        "api_secret": "test-secret",
        "approval": approval or make_approval(),
    }
    values.update(overrides)
    return RuntimeConfig(**values)


def test_offline_is_the_default_and_blocks_external_effects():
    config = RuntimeConfig()

    assert config.mode is RuntimeMode.OFFLINE
    with pytest.raises(ExternalEffectBlocked):
        config.require_account_mutation("place order")
    with pytest.raises(ExternalEffectBlocked):
        config.require_notification()


def test_offline_mode_cannot_enable_external_effects():
    with pytest.raises(RuntimeConfigurationError):
        RuntimeConfig(external_effects_enabled=True)


def test_live_external_effects_require_approval():
    with pytest.raises(RuntimeConfigurationError):
        RuntimeConfig(mode=RuntimeMode.LIVE, external_effects_enabled=True)


def test_matching_live_approval_allows_an_in_policy_request():
    config = make_live_config()

    config.require_account_mutation(
        "place order",
        symbol="btcusdt",
        side="buy",
        margin=400,
        notional=2_000,
        leverage=5,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", "ETHUSDT"),
        ("side", "HOLD"),
        ("margin", 401),
        ("notional", 2_001),
        ("leverage", 6),
    ],
)
def test_live_approval_rejects_out_of_policy_requests(field, value):
    config = make_live_config()
    request = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "margin": 400,
        "notional": 2_000,
        "leverage": 5,
    }
    request[field] = value

    with pytest.raises(ExternalEffectBlocked):
        config.require_account_mutation("place order", **request)


def test_live_approval_is_bound_to_runtime_identity():
    config = make_live_config(run_id="different-run")

    with pytest.raises(ExternalEffectBlocked, match="does not match"):
        config.require_account_mutation("place order", symbol="BTCUSDT", side="BUY")


def test_expired_live_approval_is_rejected():
    now = datetime.now(timezone.utc)
    approval = make_approval(
        approved_at=now - timedelta(minutes=2),
        expires_at=now - timedelta(minutes=1),
    )
    config = make_live_config(approval=approval)

    with pytest.raises(ExternalEffectBlocked, match="not currently valid"):
        config.require_account_mutation("place order", symbol="BTCUSDT", side="BUY")


def test_environment_loading_is_explicit_and_does_not_require_dotenv():
    config = RuntimeConfig.from_environment(
        mode=RuntimeMode.TESTNET,
        environ={"API_KEY": "key", "API_SECRET": "secret"},
    )

    assert config.mode is RuntimeMode.TESTNET
    assert config.api_key == "key"
    assert config.api_secret == "secret"


def test_configuration_errors_do_not_include_secret_values():
    config = RuntimeConfig(
        mode=RuntimeMode.TESTNET,
        external_effects_enabled=True,
        api_key="private-key-value",
    )

    with pytest.raises(RuntimeConfigurationError) as captured:
        config.require_account_mutation("place order")

    assert "private-key-value" not in str(captured.value)
