from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256

_ALLOWED_FROM_IMPORTS = {
    "__future__": {"annotations"},
    "dataclasses": {"dataclass", "field"},
    "datetime": {"datetime", "timedelta", "timezone"},
    "decimal": {"Decimal"},
    "typing": {"Any", "Mapping", "Protocol", "Sequence"},
    "promptperp.domain": {"OrderSide", "PositionSide"},
    "promptperp.strategies.base": {"RankingSnapshot", "StrategySignal"},
}
_FORBIDDEN_NAMES = {
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "eval",
    "exec",
    "getattr",
    "globals",
    "input",
    "locals",
    "open",
    "setattr",
    "vars",
}
_FORBIDDEN_TOP_LEVEL = (
    ast.Assert,
    ast.AugAssign,
    ast.Delete,
    ast.Expr,
    ast.For,
    ast.If,
    ast.Raise,
    ast.Try,
    ast.While,
    ast.With,
)
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_STRATEGY_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MAX_SOURCE_BYTES = 256 * 1024


class StrategyPackageRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class StrategyPackageManifest:
    schema_version: int
    strategy_id: str
    interface_version: int
    source_sha256: str
    parameter_fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.interface_version != 1:
            raise ValueError("unsupported strategy package schema or interface")
        if not _STRATEGY_ID.fullmatch(self.strategy_id):
            raise ValueError("strategy package identity is invalid")
        if not _HEX_64.fullmatch(self.source_sha256):
            raise ValueError("strategy source digest is invalid")
        if not _HEX_64.fullmatch(self.parameter_fingerprint):
            raise ValueError("strategy parameter fingerprint is invalid")

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()
        return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ApprovalBinding:
    schema_version: int
    approval_id: str
    strategy_id: str
    strategy_fingerprint: str
    parameter_fingerprint: str
    commit_sha: str
    environment_fingerprint: str
    risk_policy_fingerprint: str
    approved_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        fingerprints = (
            self.strategy_fingerprint,
            self.parameter_fingerprint,
            self.environment_fingerprint,
            self.risk_policy_fingerprint,
        )
        if self.schema_version != 1 or not self.approval_id:
            raise ValueError("strategy approval binding is incomplete")
        if not _STRATEGY_ID.fullmatch(self.strategy_id):
            raise ValueError("strategy approval identity is invalid")
        if not all(_HEX_64.fullmatch(value) for value in fingerprints):
            raise ValueError("strategy approval fingerprints are invalid")
        if not re.fullmatch(r"[0-9a-f]{40}", self.commit_sha):
            raise ValueError("strategy approval commit is invalid")
        if self.approved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("strategy approval timestamps must be timezone-aware")
        if self.expires_at <= self.approved_at:
            raise ValueError("strategy approval expiry must follow approval")


@dataclass(frozen=True)
class ValidationReport:
    schema_version: int
    accepted: bool
    strategy_id: str
    strategy_fingerprint: str
    approval_id: str
    checked_at: datetime
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.checked_at.tzinfo is None:
            raise ValueError("validation report schema or timestamp is invalid")
        if (
            not self.strategy_id
            or not self.strategy_fingerprint
            or not self.approval_id
        ):
            raise ValueError("validation report identity is required")
        if self.accepted != (self.reason_codes == ("ACCEPTED",)):
            raise ValueError("validation report result and reasons disagree")

    def to_json(self) -> str:
        values = asdict(self)
        values["checked_at"] = self.checked_at.isoformat()
        return json.dumps(values, sort_keys=True, separators=(",", ":"))


def validate_source(
    *,
    strategy_id: str,
    source: bytes,
    parameter_fingerprint: str,
) -> StrategyPackageManifest:
    if not _STRATEGY_ID.fullmatch(strategy_id):
        raise StrategyPackageRejected("strategy identity is invalid")
    if not _HEX_64.fullmatch(parameter_fingerprint):
        raise StrategyPackageRejected("strategy parameter fingerprint is invalid")
    if len(source) > _MAX_SOURCE_BYTES:
        raise StrategyPackageRejected("strategy source exceeds the size limit")
    try:
        text = source.decode("utf-8")
        tree = ast.parse(text, filename=f"{strategy_id}.py")
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise StrategyPackageRejected(
            "strategy source is not valid UTF-8 Python"
        ) from exc

    if any(isinstance(node, _FORBIDDEN_TOP_LEVEL) for node in tree.body):
        raise StrategyPackageRejected("strategy has forbidden top-level execution")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            raise StrategyPackageRejected("strategy imports a forbidden capability")
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            allowed = _ALLOWED_FROM_IMPORTS.get(module, set())
            imported = {alias.name for alias in node.names}
            if node.level or not imported or not imported <= allowed:
                raise StrategyPackageRejected("strategy imports a forbidden capability")
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            raise StrategyPackageRejected("strategy references a forbidden capability")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise StrategyPackageRejected(
                "strategy uses forbidden dynamic introspection"
            )
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise StrategyPackageRejected("strategy uses forbidden global state")

    return StrategyPackageManifest(
        schema_version=1,
        strategy_id=strategy_id,
        interface_version=1,
        source_sha256=sha256(source).hexdigest(),
        parameter_fingerprint=parameter_fingerprint,
    )


def verify_approval(
    *,
    manifest: StrategyPackageManifest,
    approval: ApprovalBinding,
    commit_sha: str,
    environment_fingerprint: str,
    risk_policy_fingerprint: str,
    checked_at: datetime,
) -> ValidationReport:
    if checked_at.tzinfo is None:
        raise ValueError("approval check time must be timezone-aware")
    reasons: list[str] = []
    if not approval.approved_at <= checked_at < approval.expires_at:
        reasons.append("APPROVAL_EXPIRED")
    if approval.strategy_id != manifest.strategy_id:
        reasons.append("STRATEGY_ID_MISMATCH")
    if approval.strategy_fingerprint != manifest.fingerprint:
        reasons.append("STRATEGY_VERSION_MISMATCH")
    if approval.parameter_fingerprint != manifest.parameter_fingerprint:
        reasons.append("PARAMETER_MISMATCH")
    if approval.commit_sha != commit_sha:
        reasons.append("COMMIT_MISMATCH")
    if approval.environment_fingerprint != environment_fingerprint:
        reasons.append("ENVIRONMENT_MISMATCH")
    if approval.risk_policy_fingerprint != risk_policy_fingerprint:
        reasons.append("RISK_POLICY_MISMATCH")
    return ValidationReport(
        schema_version=1,
        accepted=not reasons,
        strategy_id=manifest.strategy_id,
        strategy_fingerprint=manifest.fingerprint,
        approval_id=approval.approval_id,
        checked_at=checked_at,
        reason_codes=tuple(reasons or ["ACCEPTED"]),
    )
