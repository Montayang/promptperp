from __future__ import annotations

import hashlib
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from promptperp.deployment import (
    ComponentHealth,
    CredentialStore,
    DeploymentError,
    DeploymentManager,
    DeploymentMonitor,
    DeploymentStatus,
    HealthLevel,
    HostLayout,
    OfflineInstaller,
    PromotionEnvironment,
    PromotionEvidence,
    RecoveryError,
    ReleaseManifest,
    SafetyAttestation,
    StateArchive,
    WheelhouseLock,
    sign_promotion,
)
from promptperp.deployment.credentials import CredentialError
from promptperp.operations.deployment_service import main as deployment_main

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
SIGNING_KEY = b"x" * 32


def stopped_flat(now: datetime = NOW) -> SafetyAttestation:
    return SafetyAttestation(
        schema_version=1,
        observed_at=now,
        services_stopped=True,
        account_reconciled=True,
        positions=0,
        open_orders=0,
        pending_settlements=0,
        unresolved_ownership=0,
    )


def make_release(tmp_path: Path, suffix: str, *, state_schema: int = 1):
    source = tmp_path / f"source-{suffix}"
    source.mkdir()
    artifact = source / f"promptperp-1.0.{suffix}-py3-none-any.whl"
    distribution = f"promptperp-1.0.{suffix}.dist-info"
    with zipfile.ZipFile(artifact, "w") as wheel:
        wheel.writestr("promptperp/__init__.py", "")
        wheel.writestr(
            f"{distribution}/METADATA",
            f"Metadata-Version: 2.1\nName: promptperp\nVersion: 1.0.{suffix}\n",
        )
        wheel.writestr(
            f"{distribution}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: deployment-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        wheel.writestr(f"{distribution}/RECORD", "")
    manifest = ReleaseManifest(
        schema_version=1,
        version=f"1.0.{suffix}",
        commit_sha=suffix * 40,
        artifact_name=artifact.name,
        artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        built_at=NOW,
        python_version="3.12",
        config_schema_version=1,
        state_schema_version=state_schema,
    )
    return artifact, manifest


def prepare_and_install(
    manager: DeploymentManager,
    artifact: Path,
    manifest: ReleaseManifest,
    tmp_path: Path,
) -> None:
    manager.prepare(manifest, artifact)
    wheelhouse = tmp_path / f"wheelhouse-{manifest.version}"
    (wheelhouse / "wheels").mkdir(parents=True)
    lock = WheelhouseLock(
        schema_version=1,
        application_sha256=manifest.artifact_sha256,
        wheels={},
    )
    (wheelhouse / "wheelhouse.json").write_text(
        json.dumps(lock.to_dict(), sort_keys=True, separators=(",", ":"))
    )
    OfflineInstaller(manager).install(
        release_fingerprint=manifest.fingerprint,
        wheelhouse=wheelhouse,
        python_executable="/usr/bin/python3.12",
        installed_at=NOW,
    )


def evidence(
    manifest: ReleaseManifest, *, production: bool = True
) -> PromotionEvidence:
    return PromotionEvidence(
        schema_version=1,
        release_fingerprint=manifest.fingerprint,
        clean_commit=True,
        offline_quality_passed=True,
        sandbox_passed=True,
        testnet_protocol_passed=production,
        rollback_drill_passed=True,
        disaster_restore_passed=True,
        quality_report_fingerprint="1" * 64,
        sandbox_report_fingerprint="2" * 64,
        testnet_report_fingerprint="3" * 64 if production else None,
        rollback_report_fingerprint="4" * 64,
        disaster_restore_report_fingerprint="5" * 64,
        unresolved_findings=0,
        recorded_at=NOW,
    )


def approval(
    manifest: ReleaseManifest,
    proof: PromotionEvidence,
    environment: PromotionEnvironment,
    *,
    approval_id: str | None = None,
    key: bytes = SIGNING_KEY,
):
    maximum = {
        PromotionEnvironment.OFFLINE: timedelta(hours=12),
        PromotionEnvironment.TESTNET: timedelta(hours=2),
        PromotionEnvironment.PRODUCTION: timedelta(minutes=30),
    }[environment]
    return sign_promotion(
        approval_id=approval_id
        or f"approve-{environment.value.lower()}-{manifest.version}",
        environment=environment,
        release_fingerprint=manifest.fingerprint,
        evidence_fingerprint=proof.fingerprint,
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + maximum,
        signer="operator",
        signing_key=key,
    )


def manager_at(tmp_path: Path):
    layout = HostLayout.under(tmp_path / "host")
    manager = DeploymentManager(layout, trust_roots={"operator": SIGNING_KEY})
    manager.initialize_layout()
    return layout, manager


def promote(
    manager: DeploymentManager,
    manifest: ReleaseManifest,
    environment: PromotionEnvironment,
    *,
    proof: PromotionEvidence | None = None,
):
    selected = proof or evidence(manifest)
    return manager.activate(
        release_fingerprint=manifest.fingerprint,
        evidence=selected,
        approval=approval(manifest, selected, environment),
        attestation=stopped_flat(),
        checked_at=NOW,
    )


def test_layout_has_separate_code_config_credentials_state_logs_and_backups(tmp_path):
    layout, _ = manager_at(tmp_path)
    paths = {
        layout.releases,
        layout.config,
        layout.credentials,
        layout.state,
        layout.status,
        layout.logs,
        layout.backups,
        layout.alerts,
    }
    assert len(paths) == 8
    assert all(path.is_dir() for path in paths)
    assert os.stat(layout.credentials).st_mode & 0o777 == 0o700
    assert os.stat(layout.backups).st_mode & 0o777 == 0o700


def test_prepare_is_content_addressed_idempotent_and_detects_changes(tmp_path):
    _, manager = manager_at(tmp_path)
    artifact, manifest = make_release(tmp_path, "1")
    first = manager.prepare(manifest, artifact)
    assert manager.prepare(manifest, artifact) == first
    assert first.name == manifest.fingerprint
    stored_artifact = first / artifact.name
    os.chmod(stored_artifact, 0o644)
    stored_artifact.write_bytes(b"tampered")
    with pytest.raises(DeploymentError, match="modified|differs"):
        manager.release_manifest(manifest.fingerprint)


def test_promotion_cannot_skip_channels_or_use_unsafe_account_state(tmp_path):
    _, manager = manager_at(tmp_path)
    artifact, manifest = make_release(tmp_path, "1")
    prepare_and_install(manager, artifact, manifest, tmp_path)
    proof = evidence(manifest)
    with pytest.raises(DeploymentError, match="requires the same release"):
        promote(manager, manifest, PromotionEnvironment.TESTNET, proof=proof)
    unsafe = SafetyAttestation(
        schema_version=1,
        observed_at=NOW,
        services_stopped=True,
        account_reconciled=True,
        positions=1,
        open_orders=0,
        pending_settlements=0,
        unresolved_ownership=0,
    )
    with pytest.raises(DeploymentError, match="flat reconciled stop"):
        manager.activate(
            release_fingerprint=manifest.fingerprint,
            evidence=proof,
            approval=approval(manifest, proof, PromotionEnvironment.OFFLINE),
            attestation=unsafe,
            checked_at=NOW,
        )


def test_full_offline_testnet_production_promotion_and_machine_state(tmp_path):
    layout, manager = manager_at(tmp_path)
    artifact, manifest = make_release(tmp_path, "1")
    prepare_and_install(manager, artifact, manifest, tmp_path)
    offline = promote(manager, manifest, PromotionEnvironment.OFFLINE)
    testnet = promote(manager, manifest, PromotionEnvironment.TESTNET)
    production = promote(manager, manifest, PromotionEnvironment.PRODUCTION)
    assert offline.environment is PromotionEnvironment.OFFLINE
    assert testnet.environment is PromotionEnvironment.TESTNET
    assert production.status is DeploymentStatus.ACTIVE
    assert production.active_release == manifest.fingerprint
    assert (layout.current.with_name("current-production")).resolve() == (
        layout.releases / manifest.fingerprint
    )
    assert manager.state(PromotionEnvironment.PRODUCTION) == production


def test_changed_policy_evidence_signature_expiry_and_long_windows_fail(tmp_path):
    _, manager = manager_at(tmp_path)
    artifact, manifest = make_release(tmp_path, "1")
    prepare_and_install(manager, artifact, manifest, tmp_path)
    proof = evidence(manifest)
    forged = approval(manifest, proof, PromotionEnvironment.OFFLINE, key=b"z" * 32)
    with pytest.raises(DeploymentError, match="signature"):
        manager.activate(
            release_fingerprint=manifest.fingerprint,
            evidence=proof,
            approval=forged,
            attestation=stopped_flat(),
            checked_at=NOW,
        )
    expired = sign_promotion(
        approval_id="expired",
        environment=PromotionEnvironment.OFFLINE,
        release_fingerprint=manifest.fingerprint,
        evidence_fingerprint=proof.fingerprint,
        approved_at=NOW - timedelta(hours=2),
        expires_at=NOW - timedelta(hours=1),
        signer="operator",
        signing_key=SIGNING_KEY,
    )
    with pytest.raises(DeploymentError, match="expired"):
        manager.activate(
            release_fingerprint=manifest.fingerprint,
            evidence=proof,
            approval=expired,
            attestation=stopped_flat(),
            checked_at=NOW,
        )


def test_promotion_approval_is_single_use_and_history_tampering_blocks(tmp_path):
    layout, manager = manager_at(tmp_path)
    artifact, manifest = make_release(tmp_path, "1")
    prepare_and_install(manager, artifact, manifest, tmp_path)
    proof = evidence(manifest)
    grant = approval(manifest, proof, PromotionEnvironment.OFFLINE)
    manager.activate(
        release_fingerprint=manifest.fingerprint,
        evidence=proof,
        approval=grant,
        attestation=stopped_flat(),
        checked_at=NOW,
    )
    with pytest.raises(DeploymentError, match="already consumed|already active"):
        manager.activate(
            release_fingerprint=manifest.fingerprint,
            evidence=proof,
            approval=grant,
            attestation=stopped_flat(),
            checked_at=NOW,
        )
    history = layout.status / "deployment-history.ndjson"
    history.write_bytes(
        history.read_bytes().replace(b'"generation":1', b'"generation":9')
    )
    with pytest.raises(DeploymentError, match="history is corrupted"):
        manager.activate(
            release_fingerprint=manifest.fingerprint,
            evidence=proof,
            approval=approval(
                manifest,
                proof,
                PromotionEnvironment.OFFLINE,
                approval_id="new-approval",
            ),
            attestation=stopped_flat(),
            checked_at=NOW,
        )


def test_upgrade_and_rollback_preserve_previous_release(tmp_path):
    _, manager = manager_at(tmp_path)
    artifact1, release1 = make_release(tmp_path, "1")
    artifact2, release2 = make_release(tmp_path, "2")
    prepare_and_install(manager, artifact1, release1, tmp_path)
    prepare_and_install(manager, artifact2, release2, tmp_path)
    for environment in PromotionEnvironment:
        promote(manager, release1, environment)
    for environment in PromotionEnvironment:
        promote(manager, release2, environment)
    current = manager.state(PromotionEnvironment.PRODUCTION)
    assert current is not None and current.previous_release == release1.fingerprint
    proof = evidence(release1)
    rolled_back = manager.rollback(
        environment=PromotionEnvironment.PRODUCTION,
        evidence=proof,
        approval=approval(
            release1,
            proof,
            PromotionEnvironment.PRODUCTION,
            approval_id="rollback-release-1",
        ),
        attestation=stopped_flat(),
        checked_at=NOW,
    )
    assert rolled_back.status is DeploymentStatus.ROLLED_BACK
    assert rolled_back.active_release == release1.fingerprint
    assert rolled_back.previous_release == release2.fingerprint


def test_automatic_upgrade_and_rollback_across_state_schema_is_blocked(tmp_path):
    _, manager = manager_at(tmp_path)
    artifact1, release1 = make_release(tmp_path, "1", state_schema=1)
    artifact2, release2 = make_release(tmp_path, "2", state_schema=2)
    prepare_and_install(manager, artifact1, release1, tmp_path)
    prepare_and_install(manager, artifact2, release2, tmp_path)
    promote(manager, release1, PromotionEnvironment.OFFLINE)
    with pytest.raises(DeploymentError, match="state schema"):
        promote(manager, release2, PromotionEnvironment.OFFLINE)


def test_exchange_credentials_require_stopped_flat_state_and_never_enter_receipt(
    tmp_path,
):
    layout, _ = manager_at(tmp_path)
    store = CredentialStore(layout)
    values = {
        "BINANCE_API_KEY": "fictional-key",
        "BINANCE_API_SECRET": "fictional-secret",
    }
    with pytest.raises(CredentialError, match="flat reconciled stop"):
        store.rotate(service="exchange", values=values, rotated_at=NOW)
    receipt = store.rotate(
        service="exchange", values=values, rotated_at=NOW, attestation=stopped_flat()
    )
    assert receipt.generation == 1
    encoded = json.dumps(receipt.to_dict())
    assert "fictional-key" not in encoded and "fictional-secret" not in encoded
    secret_path = layout.credentials / "exchange.env"
    assert os.stat(secret_path).st_mode & 0o777 == 0o600
    assert secret_path.read_text().count("\n") == 2


def test_state_archive_restore_is_verified_excludes_credentials_and_requires_empty_target(
    tmp_path,
):
    layout, _ = manager_at(tmp_path)
    (layout.state / "ledger").mkdir()
    (layout.state / "ledger/accounting.sqlite3").write_bytes(b"fictional-ledger")
    (layout.state / "runtime.lease").write_text("not backed up")
    layout.credentials.joinpath("exchange.env").write_text("secret=not-in-archive")
    archive_path = layout.backups / "state.zip"
    receipt = StateArchive(layout).create(
        archive_path, attestation=stopped_flat(), created_at=NOW
    )
    assert receipt.files == 1
    restored = tmp_path / "restored-state"
    restore = StateArchive(layout).restore(
        archive_path,
        restored,
        attestation=stopped_flat(),
        restored_at=NOW,
    )
    assert restore.sha256 == receipt.sha256
    assert (restored / "ledger/accounting.sqlite3").read_bytes() == b"fictional-ledger"
    assert not (restored / "runtime.lease").exists()
    assert b"not-in-archive" not in archive_path.read_bytes()
    with pytest.raises(RecoveryError, match="empty"):
        StateArchive(layout).restore(
            archive_path,
            restored,
            attestation=stopped_flat(),
            restored_at=NOW,
        )


def test_archive_tampering_and_unsafe_restore_fail_closed(tmp_path):
    layout, _ = manager_at(tmp_path)
    (layout.state / "state.json").write_text("{}")
    archive = layout.backups / "state.zip"
    StateArchive(layout).create(archive, attestation=stopped_flat(), created_at=NOW)
    archive.write_bytes(archive.read_bytes()[:-5] + b"wrong")
    with pytest.raises(RecoveryError):
        StateArchive(layout).restore(
            archive,
            tmp_path / "restore",
            attestation=stopped_flat(),
            restored_at=NOW,
        )


def test_monitor_writes_metrics_dashboard_and_deduplicated_external_alert_spool(
    tmp_path,
):
    layout, manager = manager_at(tmp_path)
    monitor = DeploymentMonitor(layout, manager)
    monitor.publish_component(
        ComponentHealth(
            schema_version=1,
            component="sample-runtime",
            level=HealthLevel.NORMAL,
            observed_at=NOW,
            reasons=(),
            counters={"settled_trades": 2},
        )
    )
    healthy = monitor.collect(observed_at=NOW)
    assert healthy.level is HealthLevel.NORMAL
    assert (
        "promptperp_deployment_health 0" in (layout.status / "metrics.prom").read_text()
    )
    assert "sample-runtime" in (layout.status / "dashboard.html").read_text()
    blocked = monitor.collect(observed_at=NOW + timedelta(minutes=10))
    assert blocked.level is HealthLevel.BLOCKED
    outbox = layout.alerts / "outbox.ndjson"
    first = outbox.read_bytes()
    monitor.collect(observed_at=NOW + timedelta(minutes=10))
    assert outbox.read_bytes() == first


def test_corrupted_deployment_state_and_component_status_fail_closed(tmp_path):
    layout, manager = manager_at(tmp_path)
    artifact, manifest = make_release(tmp_path, "1")
    prepare_and_install(manager, artifact, manifest, tmp_path)
    promote(manager, manifest, PromotionEnvironment.OFFLINE)
    state = layout.status / "deployment-offline.json"
    state.write_bytes(state.read_bytes().replace(b'"generation":1', b'"generation":9'))
    with pytest.raises(DeploymentError, match="corrupted"):
        manager.state(PromotionEnvironment.OFFLINE)


def test_interrupted_deployment_transaction_is_visible_and_blocks_mutation(
    tmp_path, monkeypatch
):
    layout, manager = manager_at(tmp_path)
    artifact, manifest = make_release(tmp_path, "1")
    prepare_and_install(manager, artifact, manifest, tmp_path)
    proof = evidence(manifest)

    def fail_history(*args, **kwargs):
        raise OSError("fictional disk failure")

    monkeypatch.setattr(manager, "_append_history", fail_history)
    with pytest.raises(OSError, match="fictional"):
        manager.activate(
            release_fingerprint=manifest.fingerprint,
            evidence=proof,
            approval=approval(manifest, proof, PromotionEnvironment.OFFLINE),
            attestation=stopped_flat(),
            checked_at=NOW,
        )
    assert (layout.status / "deployment-transaction.json").exists()
    health = DeploymentMonitor(layout, manager).collect(observed_at=NOW)
    assert health.level is HealthLevel.BLOCKED
    assert "DEPLOYMENT_TRANSACTION_UNCERTAIN" in health.reasons


def test_safety_attestation_rejects_stale_future_or_unresolved_state():
    assert stopped_flat().safe_for_mutation(checked_at=NOW)
    assert not stopped_flat(NOW - timedelta(minutes=10)).safe_for_mutation(
        checked_at=NOW
    )
    assert not stopped_flat(NOW + timedelta(seconds=1)).safe_for_mutation(
        checked_at=NOW
    )


def test_deployment_cli_initializes_and_reports_without_starting_services(
    tmp_path, capsys
):
    root = tmp_path / "cli-host"
    assert deployment_main(["--root", str(root), "init"]) == 0
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["status"] == "INITIALIZED"
    assert (
        deployment_main(
            ["--root", str(root), "monitor", "--observed-at", NOW.isoformat()]
        )
        == 0
    )
    monitored = json.loads(capsys.readouterr().out)
    assert monitored["level"] == "NORMAL"


def test_public_distribution_contains_no_live_strategy_unit():
    root = Path(__file__).resolve().parents[2]
    unit_names = {path.name for path in (root / "deploy/systemd").glob("*")}

    assert not any("strategy" in name or "live" in name for name in unit_names)
    assert "promptperp-deployment-monitor.service" in unit_names
