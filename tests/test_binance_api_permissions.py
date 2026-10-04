from dataclasses import replace
from types import SimpleNamespace

import pytest

from promptperp.config import ExternalEffectBlocked, RuntimeConfig
from promptperp.domain import ReconciliationFailed
from promptperp.exchange.binance_api_permissions import (
    BinanceApiPermissions,
    read_api_permissions,
)


def test_read_permissions_is_get_only_and_requires_explicit_booleans():
    calls = []
    payload = dict(
        enableReading=True,
        enableFutures=True,
        enableWithdrawals=False,
        enableInternalTransfer=False,
        permitsUniversalTransfer=False,
        ipRestrict=True,
    )

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    permissions = read_api_permissions(
        RuntimeConfig(api_key="offline-key", api_secret="offline-secret"),
        transport=SimpleNamespace(get=get),
    )
    permissions.require_restricted_futures_scope()
    assert len(calls) == 1 and calls[0][0].endswith("/account/apiRestrictions")
    assert "signature" in calls[0][1]["params"]
    payload["enableWithdrawals"] = "false"
    with pytest.raises(ReconciliationFailed, match="unavailable"):
        read_api_permissions(
            RuntimeConfig(api_key="offline-key", api_secret="offline-secret"),
            transport=SimpleNamespace(get=get),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("withdrawals", True),
        ("internal_transfer", True),
        ("universal_transfer", True),
        ("ip_restricted", False),
        ("reading", False),
        ("futures", False),
    ],
)
def test_unsafe_or_incomplete_permissions_block_before_trading(field, value):
    safe = BinanceApiPermissions(True, True, False, False, False, True)
    with pytest.raises(ReconciliationFailed):
        replace(safe, **{field: value}).require_restricted_futures_scope()


def test_default_transport_cannot_make_offline_private_request():
    with pytest.raises(ExternalEffectBlocked):
        read_api_permissions(
            RuntimeConfig(api_key="offline-key", api_secret="offline-secret")
        )
