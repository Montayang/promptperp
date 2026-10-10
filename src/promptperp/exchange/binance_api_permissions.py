from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import requests  # type: ignore[import-untyped]

from promptperp.config import ExternalEffectBlocked, RuntimeConfig, RuntimeMode
from promptperp.domain import ReconciliationFailed


@dataclass(frozen=True)
class BinanceApiPermissions:
    reading: bool
    futures: bool
    withdrawals: bool
    internal_transfer: bool
    universal_transfer: bool
    ip_restricted: bool

    def require_restricted_futures_scope(self) -> None:
        if not self.reading or not self.futures or not self.ip_restricted:
            raise ReconciliationFailed(
                "Futures requires futures read/trade and an IP restriction"
            )
        if self.withdrawals or self.internal_transfer or self.universal_transfer:
            raise ReconciliationFailed(
                "Futures requires withdrawal and transfer permissions disabled"
            )


def read_api_permissions(
    config: RuntimeConfig, *, transport: Any = None, checked_at: datetime | None = None
) -> BinanceApiPermissions:
    """Read permission booleans only; there is deliberately no permission setter.

    Endpoint: Binance Wallet Get API Key Permission (USER_DATA).
    Never surface signed URLs or raw HTTP exception bodies.
    """
    if transport is None:
        if config.mode is not RuntimeMode.LIVE:
            raise ExternalEffectBlocked(
                "production permission reads require live approval"
            )
        config.require_account_mutation("inspect API permissions")
    if not config.api_key or not config.api_secret:
        raise ReconciliationFailed("permission check requires configured credentials")
    at = checked_at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError("permission observation must be timezone-aware")
    params: dict[str, object] = {
        "timestamp": int(at.timestamp() * 1000),
        "recvWindow": 5000,
    }
    params["signature"] = hmac.new(
        config.api_secret.encode(), urlencode(params).encode(), hashlib.sha256
    ).hexdigest()
    try:
        response = (transport or requests).get(
            "https://api.binance.com/sapi/v1/account/apiRestrictions",
            params=params,
            headers={"X-MBX-APIKEY": config.api_key},
            timeout=15,
        )
        response.raise_for_status()
        value = response.json()
        keys = (
            "enableReading",
            "enableFutures",
            "enableWithdrawals",
            "enableInternalTransfer",
            "permitsUniversalTransfer",
            "ipRestrict",
        )
        if not isinstance(value, dict) or any(
            type(value.get(key)) is not bool for key in keys
        ):
            raise ValueError("permission response is incomplete")
        return BinanceApiPermissions(*(value[key] for key in keys))
    except Exception:
        raise ReconciliationFailed("API permission evidence is unavailable") from None
