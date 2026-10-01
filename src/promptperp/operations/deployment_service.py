from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from promptperp.deployment import (
    CredentialStore,
    DeploymentManager,
    DeploymentMonitor,
    HostLayout,
    OfflineInstaller,
    PromotionApproval,
    PromotionEnvironment,
    PromotionEvidence,
    ReleaseManifest,
    SafetyAttestation,
    StateArchive,
)
from promptperp.deployment.models import canonical_json


def _load(path: str) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file() or target.is_symlink():
        raise ValueError(f"unsafe or missing input: {path}")
    payload = target.read_bytes()
    raw = json.loads(payload)
    if not isinstance(raw, dict) or canonical_json(raw) != payload:
        raise ValueError(f"input must be a canonical JSON object: {path}")
    return raw


def _manifest(value: Mapping[str, Any]) -> ReleaseManifest:
    _exact(
        value,
        {
            "schema_version",
            "version",
            "commit_sha",
            "artifact_name",
            "artifact_sha256",
            "built_at",
            "python_version",
            "config_schema_version",
            "state_schema_version",
        },
        "release manifest",
    )
    return ReleaseManifest.from_dict(dict(value))


def _evidence(value: Mapping[str, Any]) -> PromotionEvidence:
    _exact(
        value,
        {
            "schema_version",
            "release_fingerprint",
            "clean_commit",
            "offline_quality_passed",
            "sandbox_passed",
            "testnet_protocol_passed",
            "rollback_drill_passed",
            "disaster_restore_passed",
            "quality_report_fingerprint",
            "sandbox_report_fingerprint",
            "testnet_report_fingerprint",
            "rollback_report_fingerprint",
            "disaster_restore_report_fingerprint",
            "unresolved_findings",
            "recorded_at",
        },
        "promotion evidence",
    )
    return PromotionEvidence(
        schema_version=value["schema_version"],
        release_fingerprint=value["release_fingerprint"],
        clean_commit=value["clean_commit"],
        offline_quality_passed=value["offline_quality_passed"],
        sandbox_passed=value["sandbox_passed"],
        testnet_protocol_passed=value["testnet_protocol_passed"],
        rollback_drill_passed=value["rollback_drill_passed"],
        disaster_restore_passed=value["disaster_restore_passed"],
        quality_report_fingerprint=value["quality_report_fingerprint"],
        sandbox_report_fingerprint=value["sandbox_report_fingerprint"],
        testnet_report_fingerprint=value["testnet_report_fingerprint"],
        rollback_report_fingerprint=value["rollback_report_fingerprint"],
        disaster_restore_report_fingerprint=value[
            "disaster_restore_report_fingerprint"
        ],
        unresolved_findings=value["unresolved_findings"],
        recorded_at=datetime.fromisoformat(value["recorded_at"]),
    )


def _approval(value: Mapping[str, Any]) -> PromotionApproval:
    _exact(
        value,
        {
            "schema_version",
            "approval_id",
            "environment",
            "release_fingerprint",
            "evidence_fingerprint",
            "approved_at",
            "expires_at",
            "signer",
            "algorithm",
            "signature",
        },
        "promotion approval",
    )
    return PromotionApproval(
        schema_version=value["schema_version"],
        approval_id=value["approval_id"],
        environment=PromotionEnvironment(value["environment"]),
        release_fingerprint=value["release_fingerprint"],
        evidence_fingerprint=value["evidence_fingerprint"],
        approved_at=datetime.fromisoformat(value["approved_at"]),
        expires_at=datetime.fromisoformat(value["expires_at"]),
        signer=value["signer"],
        algorithm=value["algorithm"],
        signature=value["signature"],
    )


def _attestation(value: Mapping[str, Any]) -> SafetyAttestation:
    _exact(
        value,
        {
            "schema_version",
            "observed_at",
            "services_stopped",
            "account_reconciled",
            "positions",
            "open_orders",
            "pending_settlements",
            "unresolved_ownership",
        },
        "safety attestation",
    )
    return SafetyAttestation(
        schema_version=value["schema_version"],
        observed_at=datetime.fromisoformat(value["observed_at"]),
        services_stopped=value["services_stopped"],
        account_reconciled=value["account_reconciled"],
        positions=value["positions"],
        open_orders=value["open_orders"],
        pending_settlements=value["pending_settlements"],
        unresolved_ownership=value["unresolved_ownership"],
    )


def _exact(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{name} fields are invalid")


def _trust_roots(signer: str | None, key_file: str | None) -> dict[str, bytes]:
    if signer is None and key_file is None:
        return {}
    if not signer or not key_file:
        raise ValueError("both signer and trust-key-file are required")
    path = Path(key_file)
    if not path.is_file() or path.is_symlink():
        raise ValueError("trust key file is missing or unsafe")
    if path.stat().st_mode & 0o077:
        raise ValueError("trust key file permissions are too broad")
    key = path.read_bytes()
    if len(key) < 32:
        raise ValueError("trust key is too short")
    return {signer: key}


def _parse_env_file(path_value: str) -> dict[str, str]:
    path = Path(path_value)
    if not path.is_file() or path.is_symlink():
        raise ValueError("credential source is missing or unsafe")
    if path.stat().st_mode & 0o077:
        raise ValueError("credential source permissions are too broad")
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key in values:
            raise ValueError("credential source is malformed")
        values[key] = value
    return values


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline deployment state manager; never starts services or trades."
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--signer")
    parser.add_argument("--trust-key-file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")

    manifest = sub.add_parser("create-manifest")
    manifest.add_argument("--artifact", required=True)
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--commit-sha", required=True)
    manifest.add_argument("--built-at", required=True)
    manifest.add_argument("--python-version", required=True)
    manifest.add_argument("--config-schema-version", type=int, default=1)
    manifest.add_argument("--state-schema-version", type=int, default=1)
    manifest.add_argument("--output", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--manifest", required=True)
    prepare.add_argument("--artifact", required=True)

    install = sub.add_parser("install")
    install.add_argument("--release", required=True)
    install.add_argument("--wheelhouse", required=True)
    install.add_argument("--python-executable", default="/usr/bin/python3")
    install.add_argument("--installed-at", required=True)

    for name in ("activate", "rollback"):
        action = sub.add_parser(name)
        action.add_argument(
            "--environment",
            choices=[item.value for item in PromotionEnvironment],
            required=name == "rollback",
        )
        action.add_argument("--release", required=name == "activate")
        action.add_argument("--evidence", required=True)
        action.add_argument("--approval", required=True)
        action.add_argument("--attestation", required=True)
        action.add_argument("--checked-at", required=True)

    status = sub.add_parser("status")
    status.add_argument(
        "--environment",
        choices=[item.value for item in PromotionEnvironment],
        required=True,
    )

    monitor = sub.add_parser("monitor")
    monitor.add_argument("--observed-at")
    monitor.add_argument("--stale-after-seconds", type=int, default=300)

    backup = sub.add_parser("backup")
    backup.add_argument("--destination", required=True)
    backup.add_argument("--attestation", required=True)
    backup.add_argument("--created-at", required=True)

    restore = sub.add_parser("restore")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--target-state", required=True)
    restore.add_argument("--attestation", required=True)
    restore.add_argument("--restored-at", required=True)

    rotate = sub.add_parser("rotate-credentials")
    rotate.add_argument("--service", required=True)
    rotate.add_argument("--source-file", required=True)
    rotate.add_argument("--rotated-at", required=True)
    rotate.add_argument("--attestation")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    layout = HostLayout.under(args.root)
    trust_roots = _trust_roots(args.signer, args.trust_key_file)
    manager = DeploymentManager(layout, trust_roots=trust_roots)
    if args.command == "init":
        manager.initialize_layout()
        result: Mapping[str, Any] = {"status": "INITIALIZED", "root": str(layout.root)}
    elif args.command == "create-manifest":
        artifact = Path(args.artifact)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        release = ReleaseManifest(
            schema_version=1,
            version=args.version,
            commit_sha=args.commit_sha,
            artifact_name=artifact.name,
            artifact_sha256=digest,
            built_at=datetime.fromisoformat(args.built_at),
            python_version=args.python_version,
            config_schema_version=args.config_schema_version,
            state_schema_version=args.state_schema_version,
        )
        Path(args.output).write_bytes(canonical_json(release.to_dict()))
        result = {"status": "MANIFEST_CREATED", "release": release.fingerprint}
    elif args.command == "prepare":
        release = _manifest(_load(args.manifest))
        path = manager.prepare(release, args.artifact)
        result = {
            "status": "PREPARED",
            "release": release.fingerprint,
            "path": str(path),
        }
    elif args.command == "install":
        installation = OfflineInstaller(manager).install(
            release_fingerprint=args.release,
            wheelhouse=args.wheelhouse,
            python_executable=args.python_executable,
            installed_at=datetime.fromisoformat(args.installed_at),
        )
        result = installation.to_dict()
    elif args.command == "activate":
        activated = manager.activate(
            release_fingerprint=args.release,
            evidence=_evidence(_load(args.evidence)),
            approval=_approval(_load(args.approval)),
            attestation=_attestation(_load(args.attestation)),
            checked_at=datetime.fromisoformat(args.checked_at),
        )
        result = activated.to_dict()
    elif args.command == "rollback":
        rolled_back = manager.rollback(
            environment=PromotionEnvironment(args.environment),
            evidence=_evidence(_load(args.evidence)),
            approval=_approval(_load(args.approval)),
            attestation=_attestation(_load(args.attestation)),
            checked_at=datetime.fromisoformat(args.checked_at),
        )
        result = rolled_back.to_dict()
    elif args.command == "status":
        current_state = manager.state(PromotionEnvironment(args.environment))
        assert current_state is not None
        result = current_state.to_dict()
    elif args.command == "monitor":
        observed_at = (
            datetime.fromisoformat(args.observed_at)
            if args.observed_at
            else datetime.now(timezone.utc)
        )
        health = DeploymentMonitor(layout, manager).collect(
            observed_at=observed_at,
            stale_after_seconds=args.stale_after_seconds,
        )
        result = health.to_dict()
    elif args.command == "backup":
        backup_receipt = StateArchive(layout).create(
            args.destination,
            attestation=_attestation(_load(args.attestation)),
            created_at=datetime.fromisoformat(args.created_at),
        )
        result = {
            "status": "BACKED_UP",
            "sha256": backup_receipt.sha256,
            "files": backup_receipt.files,
            "total_bytes": backup_receipt.total_bytes,
        }
    elif args.command == "restore":
        restore_receipt = StateArchive(layout).restore(
            args.archive,
            args.target_state,
            attestation=_attestation(_load(args.attestation)),
            restored_at=datetime.fromisoformat(args.restored_at),
        )
        result = {
            "status": "RESTORED",
            "sha256": restore_receipt.sha256,
            "files": restore_receipt.files,
            "total_bytes": restore_receipt.total_bytes,
        }
    else:
        attestation = (
            _attestation(_load(args.attestation)) if args.attestation else None
        )
        credential_receipt = CredentialStore(layout).rotate(
            service=args.service,
            values=_parse_env_file(args.source_file),
            rotated_at=datetime.fromisoformat(args.rotated_at),
            attestation=attestation,
        )
        result = credential_receipt.to_dict()
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), default=str))
    if args.command == "monitor" and result.get("level") != "NORMAL":
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps(
                {"status": "BLOCKED", "error_type": type(exc).__name__},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
