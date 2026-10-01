from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping


class RuntimeMode(str, Enum):
    OFFLINE = "offline"
    TESTNET = "testnet"
    LIVE = "live"


class RuntimeConfigurationError(ValueError):
    """Raised when runtime configuration is incomplete or inconsistent."""


class ExternalEffectBlocked(RuntimeError):
    """Raised before an unapproved network or account mutation is attempted."""


@dataclass(frozen=True)
class LiveRunApproval:
    strategy_id: str
    run_id: str
    commit_sha: str
    approved_at: datetime
    expires_at: datetime
    max_margin: float
    max_notional: float
    max_leverage: int
    allowed_symbols: frozenset[str]
    allowed_sides: frozenset[str]

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.run_id or not self.commit_sha:
            raise RuntimeConfigurationError(
                "live approval requires strategy, run, and commit identifiers"
            )
        if self.approved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise RuntimeConfigurationError(
                "live approval timestamps must be timezone-aware"
            )
        if self.expires_at <= self.approved_at:
            raise RuntimeConfigurationError(
                "live approval expiry must follow approval time"
            )
        if self.max_margin <= 0 or self.max_notional <= 0 or self.max_leverage <= 0:
            raise RuntimeConfigurationError(
                "live approval risk limits must be positive"
            )
        normalized_symbols = frozenset(
            symbol.upper() for symbol in self.allowed_symbols
        )
        normalized_sides = frozenset(side.upper() for side in self.allowed_sides)
        if not normalized_symbols:
            raise RuntimeConfigurationError(
                "live approval requires at least one symbol"
            )
        if not normalized_sides or not normalized_sides <= {"BUY", "SELL"}:
            raise RuntimeConfigurationError("live approval sides must be BUY or SELL")
        object.__setattr__(self, "allowed_symbols", normalized_symbols)
        object.__setattr__(self, "allowed_sides", normalized_sides)

    def validate(
        self,
        *,
        strategy_id: str,
        run_id: str,
        commit_sha: str,
        symbol: str | None = None,
        side: str | None = None,
        margin: float | None = None,
        notional: float | None = None,
        leverage: int | None = None,
        now: datetime | None = None,
    ) -> None:
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            raise RuntimeConfigurationError(
                "approval validation time must be timezone-aware"
            )
        if current_time < self.approved_at or current_time >= self.expires_at:
            raise ExternalEffectBlocked("live approval is not currently valid")
        if (strategy_id, run_id, commit_sha) != (
            self.strategy_id,
            self.run_id,
            self.commit_sha,
        ):
            raise ExternalEffectBlocked("live approval does not match this runtime")
        if symbol is not None and symbol.upper() not in self.allowed_symbols:
            raise ExternalEffectBlocked("symbol is outside the live approval")
        if side is not None and side.upper() not in self.allowed_sides:
            raise ExternalEffectBlocked("side is outside the live approval")
        if margin is not None and float(margin) > self.max_margin:
            raise ExternalEffectBlocked("margin exceeds the live approval")
        if notional is not None and float(notional) > self.max_notional:
            raise ExternalEffectBlocked("notional exceeds the live approval")
        if leverage is not None and int(leverage) > self.max_leverage:
            raise ExternalEffectBlocked("leverage exceeds the live approval")


@dataclass(frozen=True)
class RuntimeConfig:
    mode: RuntimeMode = RuntimeMode.OFFLINE
    external_effects_enabled: bool = False
    strategy_id: str = "offline"
    run_id: str = "offline"
    commit_sha: str = "uncommitted"
    api_key: str | None = None
    api_secret: str | None = None
    base_path: str | None = None
    sender_email: str | None = None
    receiver_email: str | None = None
    email_password: str | None = None
    approval: LiveRunApproval | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RuntimeMode):
            raise RuntimeConfigurationError("mode must be a RuntimeMode value")
        if self.mode is RuntimeMode.OFFLINE and self.external_effects_enabled:
            raise RuntimeConfigurationError(
                "offline mode cannot enable external effects"
            )
        if self.mode is RuntimeMode.LIVE and self.external_effects_enabled:
            if self.approval is None:
                raise RuntimeConfigurationError(
                    "live external effects require approval"
                )

    @classmethod
    def from_environment(
        cls,
        *,
        mode: RuntimeMode,
        external_effects_enabled: bool = False,
        strategy_id: str = "offline",
        run_id: str = "offline",
        commit_sha: str = "uncommitted",
        approval: LiveRunApproval | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "RuntimeConfig":
        values = os.environ if environ is None else environ
        return cls(
            mode=mode,
            external_effects_enabled=external_effects_enabled,
            strategy_id=strategy_id,
            run_id=run_id,
            commit_sha=commit_sha,
            api_key=values.get("API_KEY"),
            api_secret=values.get("API_SECRET"),
            base_path=values.get("BASE_PATH"),
            sender_email=values.get("SENDER_EMAIL"),
            receiver_email=values.get("RECEIVER_EMAIL"),
            email_password=values.get("EMAIL_PASSWORD"),
            approval=approval,
        )

    def require_account_mutation(
        self,
        action: str,
        *,
        symbol: str | None = None,
        side: str | None = None,
        margin: float | None = None,
        notional: float | None = None,
        leverage: int | None = None,
    ) -> None:
        self._require_external_effect(action)
        if not self.api_key or not self.api_secret:
            raise RuntimeConfigurationError(
                "private API credentials are not configured"
            )
        self._validate_live_approval(
            symbol=symbol,
            side=side,
            margin=margin,
            notional=notional,
            leverage=leverage,
        )

    def require_notification(self) -> None:
        self._require_external_effect("send notification")
        if not self.sender_email or not self.receiver_email or not self.email_password:
            raise RuntimeConfigurationError("email notification is not configured")
        self._validate_live_approval()

    def _require_external_effect(self, action: str) -> None:
        if self.mode is RuntimeMode.OFFLINE or not self.external_effects_enabled:
            raise ExternalEffectBlocked(f"{action} is disabled by runtime mode")

    def _validate_live_approval(
        self,
        *,
        symbol: str | None = None,
        side: str | None = None,
        margin: float | None = None,
        notional: float | None = None,
        leverage: int | None = None,
    ) -> None:
        if self.mode is not RuntimeMode.LIVE:
            return
        if self.approval is None:
            raise ExternalEffectBlocked("live approval is missing")
        self.approval.validate(
            strategy_id=self.strategy_id,
            run_id=self.run_id,
            commit_sha=self.commit_sha,
            symbol=symbol,
            side=side,
            margin=margin,
            notional=notional,
            leverage=leverage,
        )
