from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

_HEX64 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?"
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


class PromotionEnvironment(str, Enum):
    OFFLINE = "OFFLINE"
    TESTNET = "TESTNET"
    PRODUCTION = "PRODUCTION"


class DeploymentStatus(str, Enum):
    PREPARED = "PREPARED"
    ACTIVE = "ACTIVE"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"


@dataclass(frozen=True)
class HostLayout:
    root: Path
    releases: Path
    current: Path
    config: Path
    credentials: Path
    state: Path
    status: Path
    logs: Path
    backups: Path
    alerts: Path

    @classmethod
    def under(cls, root: str | Path) -> HostLayout:
        base = Path(root).resolve()
        return cls(
            root=base,
            releases=base / "opt/promptperp/releases",
            current=base / "opt/promptperp/current",
            config=base / "etc/promptperp",
            credentials=base / "etc/promptperp/credentials",
            state=base / "var/lib/promptperp",
            status=base / "var/lib/promptperp/status",
            logs=base / "var/log/promptperp",
            backups=base / "var/backups/promptperp",
            alerts=base / "var/lib/promptperp/alerts",
        )


@dataclass(frozen=True)
class ReleaseManifest:
    schema_version: int
    version: str
    commit_sha: str
    artifact_name: str
    artifact_sha256: str
    built_at: datetime
    python_version: str
    config_schema_version: int
    state_schema_version: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or self.schema_version != 1
            or not _SEMVER.fullmatch(self.version)
        ):
            raise ValueError("release schema or semantic version is invalid")
        if not _COMMIT.fullmatch(self.commit_sha) or not _HEX64.fullmatch(
            self.artifact_sha256
        ):
            raise ValueError("release identity digest is invalid")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.artifact_name):
            raise ValueError("release artifact name is invalid")
        if self.built_at.tzinfo is None or not self.python_version:
            raise ValueError("release build identity is invalid")
        if (
            isinstance(self.config_schema_version, bool)
            or isinstance(self.state_schema_version, bool)
            or min(self.config_schema_version, self.state_schema_version) <= 0
        ):
            raise ValueError("release state schema versions must be positive")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["built_at"] = self.built_at.isoformat()
        return value

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_dict())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReleaseManifest:
        if set(value) != {
            "schema_version",
            "version",
            "commit_sha",
            "artifact_name",
            "artifact_sha256",
            "built_at",
            "python_version",
            "config_schema_version",
            "state_schema_version",
        }:
            raise ValueError("release manifest fields are invalid")
        return cls(
            schema_version=value["schema_version"],
            version=value["version"],
            commit_sha=value["commit_sha"],
            artifact_name=value["artifact_name"],
            artifact_sha256=value["artifact_sha256"],
            built_at=datetime.fromisoformat(value["built_at"]),
            python_version=value["python_version"],
            config_schema_version=value["config_schema_version"],
            state_schema_version=value["state_schema_version"],
        )


@dataclass(frozen=True)
class PromotionEvidence:
    schema_version: int
    release_fingerprint: str
    clean_commit: bool
    offline_quality_passed: bool
    sandbox_passed: bool
    testnet_protocol_passed: bool
    rollback_drill_passed: bool
    disaster_restore_passed: bool
    quality_report_fingerprint: str
    sandbox_report_fingerprint: str
    testnet_report_fingerprint: str | None
    rollback_report_fingerprint: str
    disaster_restore_report_fingerprint: str
    unresolved_findings: int
    recorded_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not _HEX64.fullmatch(self.release_fingerprint):
            raise ValueError("promotion evidence identity is invalid")
        flags = (
            self.clean_commit,
            self.offline_quality_passed,
            self.sandbox_passed,
            self.testnet_protocol_passed,
            self.rollback_drill_passed,
            self.disaster_restore_passed,
        )
        if not all(isinstance(value, bool) for value in flags):
            raise ValueError("promotion evidence flags must be boolean")
        required_reports = (
            self.quality_report_fingerprint,
            self.sandbox_report_fingerprint,
            self.rollback_report_fingerprint,
            self.disaster_restore_report_fingerprint,
        )
        if not all(_HEX64.fullmatch(value) for value in required_reports):
            raise ValueError("promotion evidence report fingerprint is invalid")
        if self.testnet_protocol_passed != (
            self.testnet_report_fingerprint is not None
        ) or (
            self.testnet_report_fingerprint is not None
            and not _HEX64.fullmatch(self.testnet_report_fingerprint)
        ):
            raise ValueError("Testnet evidence and report fingerprint disagree")
        if (
            isinstance(self.unresolved_findings, bool)
            or self.unresolved_findings < 0
            or self.recorded_at.tzinfo is None
        ):
            raise ValueError("promotion evidence state is invalid")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["recorded_at"] = self.recorded_at.isoformat()
        return value

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_dict())

    def qualifies(self, environment: PromotionEnvironment) -> bool:
        common = (
            self.clean_commit
            and self.offline_quality_passed
            and self.sandbox_passed
            and self.unresolved_findings == 0
        )
        if environment is PromotionEnvironment.OFFLINE:
            return common
        if environment is PromotionEnvironment.TESTNET:
            return (
                common and self.rollback_drill_passed and self.disaster_restore_passed
            )
        return (
            common
            and self.testnet_protocol_passed
            and self.rollback_drill_passed
            and self.disaster_restore_passed
        )


@dataclass(frozen=True)
class PromotionApproval:
    schema_version: int
    approval_id: str
    environment: PromotionEnvironment
    release_fingerprint: str
    evidence_fingerprint: str
    approved_at: datetime
    expires_at: datetime
    signer: str
    algorithm: str
    signature: str

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not self.approval_id or not self.signer:
            raise ValueError("promotion approval identity is invalid")
        if not all(
            _HEX64.fullmatch(value)
            for value in (self.release_fingerprint, self.evidence_fingerprint)
        ):
            raise ValueError("promotion approval fingerprint is invalid")
        if (
            self.approved_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or self.expires_at <= self.approved_at
        ):
            raise ValueError("promotion approval validity is invalid")
        if self.algorithm != "hmac-sha256-v1" or not _HEX64.fullmatch(self.signature):
            raise ValueError("promotion approval signature is invalid")

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "approval_id": self.approval_id,
            "environment": self.environment.value,
            "release_fingerprint": self.release_fingerprint,
            "evidence_fingerprint": self.evidence_fingerprint,
            "approved_at": self.approved_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "signer": self.signer,
            "algorithm": self.algorithm,
        }


def sign_promotion(
    *,
    approval_id: str,
    environment: PromotionEnvironment,
    release_fingerprint: str,
    evidence_fingerprint: str,
    approved_at: datetime,
    expires_at: datetime,
    signer: str,
    signing_key: bytes,
) -> PromotionApproval:
    if len(signing_key) < 32:
        raise ValueError("promotion signing key is too short")
    values = {
        "schema_version": 1,
        "approval_id": approval_id,
        "environment": environment.value,
        "release_fingerprint": release_fingerprint,
        "evidence_fingerprint": evidence_fingerprint,
        "approved_at": approved_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "signer": signer,
        "algorithm": "hmac-sha256-v1",
    }
    signature = hmac.new(
        signing_key, canonical_json(values), hashlib.sha256
    ).hexdigest()
    return PromotionApproval(
        schema_version=1,
        approval_id=approval_id,
        environment=environment,
        release_fingerprint=release_fingerprint,
        evidence_fingerprint=evidence_fingerprint,
        approved_at=approved_at,
        expires_at=expires_at,
        signer=signer,
        algorithm="hmac-sha256-v1",
        signature=signature,
    )


@dataclass(frozen=True)
class SafetyAttestation:
    schema_version: int
    observed_at: datetime
    services_stopped: bool
    account_reconciled: bool
    positions: int
    open_orders: int
    pending_settlements: int
    unresolved_ownership: int

    def __post_init__(self) -> None:
        counters = (
            self.positions,
            self.open_orders,
            self.pending_settlements,
            self.unresolved_ownership,
        )
        if not isinstance(self.services_stopped, bool) or not isinstance(
            self.account_reconciled, bool
        ):
            raise ValueError("safety attestation flags must be boolean")
        if (
            self.schema_version != 1
            or self.observed_at.tzinfo is None
            or any(isinstance(value, bool) for value in counters)
            or min(counters) < 0
        ):
            raise ValueError("safety attestation is invalid")

    def safe_for_mutation(
        self, *, checked_at: datetime, maximum_age_seconds: int = 300
    ) -> bool:
        age = (checked_at - self.observed_at).total_seconds()
        return (
            0 <= age <= maximum_age_seconds
            and self.services_stopped
            and self.account_reconciled
            and self.positions == 0
            and self.open_orders == 0
            and self.pending_settlements == 0
            and self.unresolved_ownership == 0
        )


@dataclass(frozen=True)
class DeploymentState:
    schema_version: int
    generation: int
    status: DeploymentStatus
    environment: PromotionEnvironment
    active_release: str
    previous_release: str | None
    approval_id: str
    updated_at: datetime

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or isinstance(self.generation, bool)
            or self.generation <= 0
        ):
            raise ValueError("deployment state schema or generation is invalid")
        if not _HEX64.fullmatch(self.active_release):
            raise ValueError("deployment active release is invalid")
        if self.previous_release is not None and not _HEX64.fullmatch(
            self.previous_release
        ):
            raise ValueError("deployment previous release is invalid")
        if not self.approval_id or self.updated_at.tzinfo is None:
            raise ValueError("deployment approval or time is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generation": self.generation,
            "status": self.status.value,
            "environment": self.environment.value,
            "active_release": self.active_release,
            "previous_release": self.previous_release,
            "approval_id": self.approval_id,
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DeploymentState:
        if set(value) != {
            "schema_version",
            "generation",
            "status",
            "environment",
            "active_release",
            "previous_release",
            "approval_id",
            "updated_at",
        }:
            raise ValueError("deployment state fields are invalid")
        return cls(
            schema_version=value["schema_version"],
            generation=value["generation"],
            status=DeploymentStatus(value["status"]),
            environment=PromotionEnvironment(value["environment"]),
            active_release=value["active_release"],
            previous_release=value["previous_release"],
            approval_id=value["approval_id"],
            updated_at=datetime.fromisoformat(value["updated_at"]),
        )
