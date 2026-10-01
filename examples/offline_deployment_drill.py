from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from promptperp.deployment import (
    ComponentHealth,
    DeploymentManager,
    DeploymentMonitor,
    HealthLevel,
    HostLayout,
    OfflineInstaller,
    PromotionEnvironment,
    PromotionEvidence,
    ReleaseManifest,
    SafetyAttestation,
    StateArchive,
    WheelhouseLock,
    sign_promotion,
)

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
KEY = b"x" * 32


def _wheel(root: Path, patch: int) -> tuple[Path, ReleaseManifest]:
    artifact = root / f"promptperp-1.0.{patch}-py3-none-any.whl"
    metadata = f"promptperp-1.0.{patch}.dist-info"
    with zipfile.ZipFile(artifact, "w") as wheel:
        wheel.writestr("promptperp/__init__.py", "")
        wheel.writestr(
            f"{metadata}/METADATA",
            f"Metadata-Version: 2.1\nName: promptperp\nVersion: 1.0.{patch}\n",
        )
        wheel.writestr(
            f"{metadata}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: offline-drill\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        wheel.writestr(f"{metadata}/RECORD", "")
    return artifact, ReleaseManifest(
        schema_version=1,
        version=f"1.0.{patch}",
        commit_sha=str(patch) * 40,
        artifact_name=artifact.name,
        artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        built_at=NOW,
        python_version="3.12",
        config_schema_version=1,
        state_schema_version=1,
    )


def _attestation() -> SafetyAttestation:
    return SafetyAttestation(1, NOW, True, True, 0, 0, 0, 0)


def _evidence(release: ReleaseManifest) -> PromotionEvidence:
    return PromotionEvidence(
        schema_version=1,
        release_fingerprint=release.fingerprint,
        clean_commit=True,
        offline_quality_passed=True,
        sandbox_passed=True,
        testnet_protocol_passed=False,
        rollback_drill_passed=True,
        disaster_restore_passed=True,
        quality_report_fingerprint="1" * 64,
        sandbox_report_fingerprint="2" * 64,
        testnet_report_fingerprint=None,
        rollback_report_fingerprint="4" * 64,
        disaster_restore_report_fingerprint="5" * 64,
        unresolved_findings=0,
        recorded_at=NOW,
    )


def _approval(release: ReleaseManifest, evidence: PromotionEvidence, name: str):
    return sign_promotion(
        approval_id=name,
        environment=PromotionEnvironment.OFFLINE,
        release_fingerprint=release.fingerprint,
        evidence_fingerprint=evidence.fingerprint,
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        signer="offline-drill",
        signing_key=KEY,
    )


def _install(
    root: Path,
    manager: DeploymentManager,
    artifact: Path,
    release: ReleaseManifest,
) -> None:
    manager.prepare(release, artifact)
    wheelhouse = root / f"wheelhouse-{release.version}"
    (wheelhouse / "wheels").mkdir(parents=True)
    lock = WheelhouseLock(1, release.artifact_sha256, {})
    (wheelhouse / "wheelhouse.json").write_text(
        json.dumps(lock.to_dict(), sort_keys=True, separators=(",", ":"))
    )
    OfflineInstaller(manager).install(
        release_fingerprint=release.fingerprint,
        wheelhouse=wheelhouse,
        python_executable="/usr/bin/python3.12",
        installed_at=NOW,
    )


def main() -> None:
    with tempfile.TemporaryDirectory(
        prefix="promptperp-deployment-drill-"
    ) as temporary:
        root = Path(temporary)
        layout = HostLayout.under(root / "host")
        manager = DeploymentManager(layout, trust_roots={"offline-drill": KEY})
        manager.initialize_layout()
        artifact1, release1 = _wheel(root, 1)
        artifact2, release2 = _wheel(root, 2)
        _install(root, manager, artifact1, release1)
        _install(root, manager, artifact2, release2)
        proof1, proof2 = _evidence(release1), _evidence(release2)
        manager.activate(
            release_fingerprint=release1.fingerprint,
            evidence=proof1,
            approval=_approval(release1, proof1, "offline-1"),
            attestation=_attestation(),
            checked_at=NOW,
        )
        manager.activate(
            release_fingerprint=release2.fingerprint,
            evidence=proof2,
            approval=_approval(release2, proof2, "offline-2"),
            attestation=_attestation(),
            checked_at=NOW,
        )
        rolled_back = manager.rollback(
            environment=PromotionEnvironment.OFFLINE,
            evidence=proof1,
            approval=_approval(release1, proof1, "offline-rollback"),
            attestation=_attestation(),
            checked_at=NOW,
        )
        (layout.state / "example-state.json").write_text("{}")
        archive = StateArchive(layout).create(
            layout.backups / "state.zip",
            attestation=_attestation(),
            created_at=NOW,
        )
        restored = StateArchive(layout).restore(
            archive.archive,
            root / "restored-state",
            attestation=_attestation(),
            restored_at=NOW,
        )
        monitor = DeploymentMonitor(layout, manager)
        monitor.publish_component(
            ComponentHealth(
                1, "offline-drill", HealthLevel.NORMAL, NOW, (), {"checks": 1}
            )
        )
        health = monitor.collect(observed_at=NOW)
        print(
            json.dumps(
                {
                    "mode": "offline",
                    "active_version": manager.release_manifest(
                        rolled_back.active_release
                    ).version,
                    "health": health.level.value,
                    "restored_files": restored.files,
                    "testnet_or_live_started": False,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
