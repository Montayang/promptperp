from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Mapping

from promptperp.deployment.models import (
    DeploymentState,
    DeploymentStatus,
    HostLayout,
    PromotionApproval,
    PromotionEnvironment,
    PromotionEvidence,
    ReleaseManifest,
    SafetyAttestation,
    canonical_json,
)


class DeploymentError(RuntimeError):
    pass


class DeploymentManager:
    def __init__(self, layout: HostLayout, *, trust_roots: Mapping[str, bytes]):
        self.layout = layout
        self.trust_roots = dict(trust_roots)

    def initialize_layout(self) -> None:
        modes = {
            self.layout.releases: 0o755,
            self.layout.config: 0o750,
            self.layout.credentials: 0o700,
            self.layout.state: 0o750,
            self.layout.status: 0o750,
            self.layout.logs: 0o750,
            self.layout.backups: 0o700,
            self.layout.alerts: 0o700,
        }
        for path, mode in modes.items():
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, mode)

    def prepare(self, manifest: ReleaseManifest, artifact: str | Path) -> Path:
        source = Path(artifact)
        if (
            not source.is_file()
            or source.is_symlink()
            or source.name != manifest.artifact_name
        ):
            raise DeploymentError("release artifact is missing, linked, or misnamed")
        if self._file_digest(source) != manifest.artifact_sha256:
            raise DeploymentError("release artifact digest mismatch")
        self.initialize_layout()
        destination = self.layout.releases / manifest.fingerprint
        with self._lock():
            if destination.exists():
                stored = self.release_manifest(manifest.fingerprint)
                if (
                    stored != manifest
                    or self._file_digest(destination / manifest.artifact_name)
                    != manifest.artifact_sha256
                ):
                    raise DeploymentError(
                        "prepared release differs from immutable artifact"
                    )
                return destination
            temporary = Path(
                tempfile.mkdtemp(prefix=".release-", dir=self.layout.releases)
            )
            try:
                shutil.copyfile(source, temporary / manifest.artifact_name)
                (temporary / "release.json").write_bytes(
                    canonical_json(manifest.to_dict())
                )
                os.chmod(temporary / manifest.artifact_name, 0o444)
                os.chmod(temporary / "release.json", 0o444)
                # Runtime identities receive this tree read-only. The deployment
                # owner retains directory write permission for explicit retirement.
                os.chmod(temporary, 0o755)
                os.rename(temporary, destination)
            except Exception:
                if temporary.exists():
                    os.chmod(temporary, 0o700)
                    shutil.rmtree(temporary)
                raise
        return destination

    def activate(
        self,
        *,
        release_fingerprint: str,
        evidence: PromotionEvidence,
        approval: PromotionApproval,
        attestation: SafetyAttestation,
        checked_at: datetime,
    ) -> DeploymentState:
        self.initialize_layout()
        manifest = self.release_manifest(release_fingerprint)
        self._verify_gate(manifest, evidence, approval, attestation, checked_at)
        environment = approval.environment
        with self._lock():
            self._require_no_uncertain_transaction()
            if self._approval_used(approval.approval_id):
                raise DeploymentError("promotion approval was already consumed")
            self._verify_prerequisite(environment, release_fingerprint)
            current = self.state(environment, required=False)
            if current and current.active_release == release_fingerprint:
                raise DeploymentError("release is already active in this environment")
            if current:
                previous_manifest = self.release_manifest(current.active_release)
                if (
                    previous_manifest.state_schema_version
                    != manifest.state_schema_version
                ):
                    raise DeploymentError(
                        "state schema change requires a dedicated migration and rollback plan"
                    )
            state = DeploymentState(
                schema_version=1,
                generation=1 if current is None else current.generation + 1,
                status=DeploymentStatus.ACTIVE,
                environment=environment,
                active_release=release_fingerprint,
                previous_release=None if current is None else current.active_release,
                approval_id=approval.approval_id,
                updated_at=checked_at,
            )
            self._begin_transaction("ACTIVATE", state)
            self._switch_link(environment, release_fingerprint)
            self._write_state(state)
            self._append_history("ACTIVATE", state, evidence.fingerprint)
            self._finish_transaction()
            return state

    def rollback(
        self,
        *,
        environment: PromotionEnvironment,
        evidence: PromotionEvidence,
        approval: PromotionApproval,
        attestation: SafetyAttestation,
        checked_at: datetime,
    ) -> DeploymentState:
        current = self.state(environment)
        assert current is not None
        if approval.environment is not environment:
            raise DeploymentError("rollback approval environment mismatch")
        if current.previous_release is None:
            raise DeploymentError("deployment has no previous release to roll back to")
        target = self.release_manifest(current.previous_release)
        self._verify_gate(target, evidence, approval, attestation, checked_at)
        current_manifest = self.release_manifest(current.active_release)
        if target.state_schema_version != current_manifest.state_schema_version:
            raise DeploymentError(
                "automatic rollback across state schemas is forbidden"
            )
        with self._lock():
            self._require_no_uncertain_transaction()
            if self._approval_used(approval.approval_id):
                raise DeploymentError("promotion approval was already consumed")
            latest = self.state(environment)
            assert latest is not None
            if latest != current:
                raise DeploymentError(
                    "deployment changed before rollback acquired the lock"
                )
            state = DeploymentState(
                schema_version=1,
                generation=current.generation + 1,
                status=DeploymentStatus.ROLLED_BACK,
                environment=environment,
                active_release=target.fingerprint,
                previous_release=current.active_release,
                approval_id=approval.approval_id,
                updated_at=checked_at,
            )
            self._begin_transaction("ROLLBACK", state)
            self._switch_link(environment, target.fingerprint)
            self._write_state(state)
            self._append_history("ROLLBACK", state, evidence.fingerprint)
            self._finish_transaction()
            return state

    def state(
        self, environment: PromotionEnvironment, *, required: bool = True
    ) -> DeploymentState | None:
        path = self._state_path(environment)
        if not path.exists():
            if required:
                raise DeploymentError(
                    f"{environment.value} deployment state is missing"
                )
            return None
        try:
            envelope = json.loads(path.read_bytes())
            payload = envelope["payload"]
            digest = envelope["sha256"]
            if hashlib.sha256(canonical_json(payload)).hexdigest() != digest:
                raise ValueError
            state = DeploymentState.from_dict(payload)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise DeploymentError("deployment state is corrupted") from exc
        if state.environment is not environment:
            raise DeploymentError("deployment state environment mismatch")
        link = self._link_path(environment)
        if not link.is_symlink():
            raise DeploymentError("active release link is missing")
        try:
            resolved = link.resolve(strict=True)
        except OSError as exc:
            raise DeploymentError("active release link is broken") from exc
        if resolved != (self.layout.releases / state.active_release).resolve():
            raise DeploymentError("active release link and state disagree")
        return state

    def release_manifest(self, fingerprint: str) -> ReleaseManifest:
        root = self.layout.releases / fingerprint
        path = root / "release.json"
        if (
            not root.is_dir()
            or root.is_symlink()
            or not path.is_file()
            or path.is_symlink()
        ):
            raise DeploymentError("prepared release is missing or unsafe")
        try:
            payload = path.read_bytes()
            raw = json.loads(payload)
            if canonical_json(raw) != payload:
                raise ValueError
            manifest = ReleaseManifest.from_dict(raw)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise DeploymentError("release manifest is malformed") from exc
        if manifest.fingerprint != fingerprint:
            raise DeploymentError("release directory is not content-addressed")
        artifact = root / manifest.artifact_name
        if (
            artifact.is_symlink()
            or self._file_digest(artifact) != manifest.artifact_sha256
        ):
            raise DeploymentError("prepared release artifact was modified")
        expected = {"release.json", manifest.artifact_name}
        runtime = root / "runtime"
        if runtime.exists():
            expected.add("runtime")
            if runtime.is_symlink() or not (runtime / ".venv/bin/python").is_file():
                raise DeploymentError("installed release runtime is missing or unsafe")
            receipt = runtime / "installation.json"
            try:
                payload = receipt.read_bytes()
                raw = json.loads(payload)
                if (
                    canonical_json(raw) != payload
                    or raw.get("release_fingerprint") != fingerprint
                ):
                    raise ValueError
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise DeploymentError("installation receipt is malformed") from exc
        if set(item.name for item in root.iterdir()) != expected:
            raise DeploymentError("prepared release has unexpected files")
        return manifest

    def verify_history(self) -> int:
        """Verify the complete hash chain without changing deployment state."""
        return len(self._history_events())

    def _verify_gate(
        self,
        manifest: ReleaseManifest,
        evidence: PromotionEvidence,
        approval: PromotionApproval,
        attestation: SafetyAttestation,
        checked_at: datetime,
    ) -> None:
        if checked_at.tzinfo is None:
            raise ValueError("deployment check time must be timezone-aware")
        runtime = self.layout.releases / manifest.fingerprint / "runtime"
        if not (runtime / ".venv/bin/python").is_file():
            raise DeploymentError(
                "release must pass offline installation before promotion"
            )
        if (
            manifest.fingerprint != evidence.release_fingerprint
            or not evidence.qualifies(approval.environment)
        ):
            raise DeploymentError(
                "release evidence does not qualify for this environment"
            )
        evidence_age = (checked_at - evidence.recorded_at).total_seconds()
        if evidence_age < 0 or evidence_age > 30 * 86400:
            raise DeploymentError("promotion evidence is future-dated or stale")
        if (
            approval.release_fingerprint != manifest.fingerprint
            or approval.evidence_fingerprint != evidence.fingerprint
        ):
            raise DeploymentError("promotion approval binding mismatch")
        if not approval.approved_at <= checked_at < approval.expires_at:
            raise DeploymentError("promotion approval is expired or not yet valid")
        maximum_window = {
            PromotionEnvironment.OFFLINE: 86400,
            PromotionEnvironment.TESTNET: 14400,
            PromotionEnvironment.PRODUCTION: 3600,
        }[approval.environment]
        if (
            approval.expires_at - approval.approved_at
        ).total_seconds() > maximum_window:
            raise DeploymentError("promotion approval window exceeds environment limit")
        key = self.trust_roots.get(approval.signer)
        if key is None or len(key) < 32:
            raise DeploymentError("promotion signer is not trusted")
        expected = hmac.new(
            key, canonical_json(approval.unsigned_dict()), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, approval.signature):
            raise DeploymentError("promotion approval signature is invalid")
        if not attestation.safe_for_mutation(checked_at=checked_at):
            raise DeploymentError(
                "deployment mutation requires a fresh flat reconciled stop"
            )

    def _verify_prerequisite(
        self, environment: PromotionEnvironment, release_fingerprint: str
    ) -> None:
        prerequisite = {
            PromotionEnvironment.OFFLINE: None,
            PromotionEnvironment.TESTNET: PromotionEnvironment.OFFLINE,
            PromotionEnvironment.PRODUCTION: PromotionEnvironment.TESTNET,
        }[environment]
        if prerequisite is None:
            return
        state = self.state(prerequisite, required=False)
        if state is None or state.active_release != release_fingerprint:
            raise DeploymentError(
                f"{environment.value} promotion requires the same release in {prerequisite.value}"
            )

    def _switch_link(self, environment: PromotionEnvironment, fingerprint: str) -> None:
        link = self._link_path(environment)
        temporary = link.with_name(f".{link.name}.new")
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(
            self.layout.releases / fingerprint, target_is_directory=True
        )
        os.replace(temporary, link)

    def _write_state(self, state: DeploymentState) -> None:
        payload = state.to_dict()
        envelope = {
            "payload": payload,
            "sha256": hashlib.sha256(canonical_json(payload)).hexdigest(),
        }
        self._atomic_write(
            self._state_path(state.environment), canonical_json(envelope), 0o600
        )

    def _begin_transaction(self, action: str, state: DeploymentState) -> None:
        payload = {
            "schema_version": 1,
            "action": action,
            "environment": state.environment.value,
            "target_release": state.active_release,
            "approval_id": state.approval_id,
            "started_at": state.updated_at.isoformat(),
        }
        self._atomic_write(self._transaction_path(), canonical_json(payload), 0o600)

    def _finish_transaction(self) -> None:
        self._transaction_path().unlink()

    def _require_no_uncertain_transaction(self) -> None:
        if self._transaction_path().exists():
            raise DeploymentError(
                "an interrupted deployment transaction requires operator reconciliation"
            )

    def _append_history(
        self, action: str, state: DeploymentState, evidence_fingerprint: str
    ) -> None:
        path = self.layout.status / "deployment-history.ndjson"
        events = self._history_events()
        previous = events[-1]["event_hash"] if events else "0" * 64
        event = {
            "action": action,
            "environment": state.environment.value,
            "generation": state.generation,
            "release": state.active_release,
            "approval_id": state.approval_id,
            "evidence": evidence_fingerprint,
            "occurred_at": state.updated_at.isoformat(),
            "previous_hash": previous,
        }
        event["event_hash"] = hashlib.sha256(canonical_json(event)).hexdigest()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, canonical_json(event) + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _approval_used(self, approval_id: str) -> bool:
        if any(
            event.get("approval_id") == approval_id for event in self._history_events()
        ):
            return True
        for environment in PromotionEnvironment:
            path = self._state_path(environment)
            if not path.exists():
                continue
            state = self.state(environment)
            if state is not None and state.approval_id == approval_id:
                return True
        return False

    def _history_events(self) -> list[dict[str, object]]:
        path = self.layout.status / "deployment-history.ndjson"
        if not path.exists():
            return []
        previous = "0" * 64
        events: list[dict[str, object]] = []
        try:
            for line in path.read_bytes().splitlines():
                event = json.loads(line)
                if (
                    not isinstance(event, dict)
                    or event.get("previous_hash") != previous
                ):
                    raise ValueError
                event_hash = event.get("event_hash")
                unsigned = dict(event)
                unsigned.pop("event_hash", None)
                expected = hashlib.sha256(canonical_json(unsigned)).hexdigest()
                if not isinstance(event_hash, str) or not hmac.compare_digest(
                    expected, event_hash
                ):
                    raise ValueError
                previous = event_hash
                events.append(event)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise DeploymentError("deployment history is corrupted") from exc
        return events

    def _state_path(self, environment: PromotionEnvironment) -> Path:
        return self.layout.status / f"deployment-{environment.value.lower()}.json"

    def _link_path(self, environment: PromotionEnvironment) -> Path:
        return self.layout.current.with_name(f"current-{environment.value.lower()}")

    def _transaction_path(self) -> Path:
        return self.layout.status / "deployment-transaction.json"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.layout.status.mkdir(parents=True, exist_ok=True)
        path = self.layout.status / "deployment.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise DeploymentError("release artifact cannot be read") from exc
        return digest.hexdigest()

    @staticmethod
    def _atomic_write(path: Path, payload: bytes, mode: int) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}-", dir=path.parent
        )
        try:
            os.fchmod(descriptor, mode)
            os.write(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if os.path.exists(temporary):
                os.unlink(temporary)
