from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from promptperp.evaluation import EvaluationReport
from promptperp.strategy_spec import StrategySpec, canonical_json, load_strategy_spec

_HEX64 = re.compile(r"[0-9a-f]{64}")
_FILES = {"spec.json", "evaluation.json", "manifest.json", "provenance.json"}


class BundleRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class BundleManifest:
    schema_version: int
    strategy_id: str
    spec_fingerprint: str
    evaluation_fingerprint: str
    interpreter_id: str
    commit_sha: str
    dependencies: tuple[str, ...]
    files: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not self.strategy_id or not self.interpreter_id:
            raise ValueError("bundle manifest identity is invalid")
        digests = (
            self.spec_fingerprint,
            self.evaluation_fingerprint,
            *self.files.values(),
        )
        if not all(_HEX64.fullmatch(value) for value in digests):
            raise ValueError("bundle manifest digest is invalid")
        if not re.fullmatch(r"[0-9a-f]{40}", self.commit_sha):
            raise ValueError("bundle commit is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "strategy_id": self.strategy_id,
            "spec_fingerprint": self.spec_fingerprint,
            "evaluation_fingerprint": self.evaluation_fingerprint,
            "interpreter_id": self.interpreter_id,
            "commit_sha": self.commit_sha,
            "dependencies": list(self.dependencies),
            "files": dict(sorted(self.files.items())),
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict())).hexdigest()


@dataclass(frozen=True)
class Provenance:
    schema_version: int
    algorithm: str
    issuer: str
    manifest_fingerprint: str
    issued_at: datetime
    expires_at: datetime
    signature: str

    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema_version": self.schema_version,
                "algorithm": self.algorithm,
                "issuer": self.issuer,
                "manifest_fingerprint": self.manifest_fingerprint,
                "issued_at": self.issued_at.isoformat(),
                "expires_at": self.expires_at.isoformat(),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        decoded = json.loads(self.payload())
        if not isinstance(decoded, dict):
            raise ValueError("provenance payload is invalid")
        value: dict[str, Any] = decoded
        value["signature"] = self.signature
        return value


@dataclass(frozen=True)
class VerifiedBundle:
    root: Path
    spec: StrategySpec
    evaluation: EvaluationReport
    manifest: BundleManifest
    provenance: Provenance


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sign_provenance(
    manifest: BundleManifest,
    *,
    issuer: str,
    signing_key: bytes,
    issued_at: datetime,
    expires_at: datetime,
) -> Provenance:
    if not issuer or len(signing_key) < 32:
        raise ValueError("provenance issuer or key is invalid")
    if issued_at.tzinfo is None or expires_at.tzinfo is None or expires_at <= issued_at:
        raise ValueError("provenance validity window is invalid")
    unsigned = Provenance(
        schema_version=1,
        algorithm="hmac-sha256-v1",
        issuer=issuer,
        manifest_fingerprint=manifest.fingerprint,
        issued_at=issued_at,
        expires_at=expires_at,
        signature="",
    )
    signature = hmac.new(signing_key, unsigned.payload(), hashlib.sha256).hexdigest()
    return Provenance(**{**unsigned.__dict__, "signature": signature})


def build_bundle(
    root: str | Path,
    *,
    spec: StrategySpec,
    evaluation: EvaluationReport,
    commit_sha: str,
    issuer: str,
    signing_key: bytes,
    issued_at: datetime,
    expires_at: datetime,
) -> BundleManifest:
    if not evaluation.passed or evaluation.spec_fingerprint != spec.fingerprint:
        raise BundleRejected("only a passing evaluation for this spec can be bundled")
    destination = Path(root)
    if destination.exists():
        raise BundleRejected("bundle destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".bundle-", dir=destination.parent))
    try:
        spec_bytes = spec.canonical_bytes
        evaluation_bytes = evaluation.to_json()
        _write_private(temporary / "spec.json", spec_bytes)
        _write_private(temporary / "evaluation.json", evaluation_bytes)
        manifest = BundleManifest(
            schema_version=1,
            strategy_id=spec.strategy_id,
            spec_fingerprint=spec.fingerprint,
            evaluation_fingerprint=evaluation.fingerprint,
            interpreter_id="promptperp-declarative-v1",
            commit_sha=commit_sha,
            dependencies=("python-stdlib",),
            files={
                "spec.json": _digest(spec_bytes),
                "evaluation.json": _digest(evaluation_bytes),
            },
        )
        _write_private(temporary / "manifest.json", canonical_json(manifest.to_dict()))
        provenance = sign_provenance(
            manifest,
            issuer=issuer,
            signing_key=signing_key,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        _write_private(
            temporary / "provenance.json", canonical_json(provenance.to_dict())
        )
        os.chmod(temporary, 0o700)
        os.rename(temporary, destination)
        for child in destination.iterdir():
            child.chmod(0o400)
        # The directory remains owner-writable so the operator can retire the
        # artifact. Payload files are read-only and every byte is hash-bound.
        destination.chmod(0o700)
        return manifest
    except Exception:
        for child in temporary.iterdir():
            child.unlink()
        temporary.rmdir()
        raise


def _load_manifest(payload: bytes) -> BundleManifest:
    try:
        raw = json.loads(payload)
        if set(raw) != {
            "schema_version",
            "strategy_id",
            "spec_fingerprint",
            "evaluation_fingerprint",
            "interpreter_id",
            "commit_sha",
            "dependencies",
            "files",
        }:
            raise ValueError
        return BundleManifest(
            schema_version=raw["schema_version"],
            strategy_id=raw["strategy_id"],
            spec_fingerprint=raw["spec_fingerprint"],
            evaluation_fingerprint=raw["evaluation_fingerprint"],
            interpreter_id=raw["interpreter_id"],
            commit_sha=raw["commit_sha"],
            dependencies=tuple(raw["dependencies"]),
            files=dict(raw["files"]),
        )
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BundleRejected("bundle manifest is malformed") from exc


def _load_provenance(payload: bytes) -> Provenance:
    try:
        raw = json.loads(payload)
        if set(raw) != {
            "schema_version",
            "algorithm",
            "issuer",
            "manifest_fingerprint",
            "issued_at",
            "expires_at",
            "signature",
        }:
            raise ValueError
        return Provenance(
            schema_version=raw["schema_version"],
            algorithm=raw["algorithm"],
            issuer=raw["issuer"],
            manifest_fingerprint=raw["manifest_fingerprint"],
            issued_at=datetime.fromisoformat(raw["issued_at"]),
            expires_at=datetime.fromisoformat(raw["expires_at"]),
            signature=raw["signature"],
        )
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BundleRejected("bundle provenance is malformed") from exc


def verify_bundle(
    root: str | Path,
    *,
    trust_roots: Mapping[str, bytes],
    checked_at: datetime,
) -> VerifiedBundle:
    if checked_at.tzinfo is None:
        raise ValueError("bundle verification time must be timezone-aware")
    path = Path(root)
    if not path.is_dir() or path.is_symlink():
        raise BundleRejected("bundle root must be a real directory")
    children = {child.name for child in path.iterdir()}
    if children != _FILES or any(
        child.is_symlink() or not child.is_file() for child in path.iterdir()
    ):
        raise BundleRejected("bundle has missing, extra, or linked files")
    manifest_bytes = (path / "manifest.json").read_bytes()
    if canonical_json(json.loads(manifest_bytes)) != manifest_bytes:
        raise BundleRejected("bundle manifest is not canonical")
    manifest = _load_manifest(manifest_bytes)
    if set(manifest.files) != {"spec.json", "evaluation.json"}:
        raise BundleRejected("bundle payload manifest is incomplete")
    for name, expected in manifest.files.items():
        if _digest((path / name).read_bytes()) != expected:
            raise BundleRejected("bundle payload digest mismatch")
    spec = load_strategy_spec((path / "spec.json").read_bytes())
    evaluation = EvaluationReport.from_json((path / "evaluation.json").read_bytes())
    if spec.fingerprint != manifest.spec_fingerprint:
        raise BundleRejected("bundle spec fingerprint mismatch")
    if (
        evaluation.fingerprint != manifest.evaluation_fingerprint
        or not evaluation.passed
    ):
        raise BundleRejected("bundle evaluation is invalid")
    if evaluation.spec_fingerprint != spec.fingerprint:
        raise BundleRejected("evaluation does not bind the bundled spec")
    provenance = _load_provenance((path / "provenance.json").read_bytes())
    if provenance.schema_version != 1 or provenance.algorithm != "hmac-sha256-v1":
        raise BundleRejected("provenance algorithm is unsupported")
    if provenance.manifest_fingerprint != manifest.fingerprint:
        raise BundleRejected("provenance does not bind the manifest")
    if provenance.issued_at.tzinfo is None or provenance.expires_at.tzinfo is None:
        raise BundleRejected("provenance time is not timezone-aware")
    if not provenance.issued_at <= checked_at < provenance.expires_at:
        raise BundleRejected("provenance is expired or not yet valid")
    key = trust_roots.get(provenance.issuer)
    if key is None:
        raise BundleRejected("provenance issuer is not trusted")
    expected_signature = hmac.new(key, provenance.payload(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_signature, provenance.signature):
        raise BundleRejected("provenance signature is invalid")
    return VerifiedBundle(path, spec, evaluation, manifest, provenance)
