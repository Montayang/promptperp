from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, cast

from promptperp.storage import SQLiteAccountingStore
from promptperp.storage.reporting_sqlite import SQLiteReportingStore


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def create_verified_backup(
    store: SQLiteAccountingStore,
    *,
    destination: Path,
    occurred_at: datetime,
) -> Path:
    if occurred_at.tzinfo is None:
        raise ValueError("backup timestamp must be timezone-aware")
    store.integrity_check()
    store.audit_ledger()
    expected = store.operational_fingerprint()
    store.backup(destination)
    recovered = SQLiteAccountingStore(destination)
    recovered.integrity_check()
    recovered.audit_ledger()
    actual = recovered.operational_fingerprint()
    if actual != expected:
        raise RuntimeError("backup fingerprint does not match the source database")
    manifest = destination.with_suffix(destination.suffix + ".manifest.json")
    _write_private_json(
        manifest,
        {
            "schema_version": 1,
            "created_at": occurred_at.astimezone(timezone.utc).isoformat(),
            "backup_file": destination.name,
            "size_bytes": destination.stat().st_size,
            "sha256": _sha256(destination),
            "database_fingerprint": actual,
        },
    )
    return manifest


def verify_backup_manifest(
    manifest_path: Path,
    *,
    now: datetime,
    maximum_age: timedelta,
) -> Mapping[str, Any]:
    if now.tzinfo is None or maximum_age <= timedelta(0):
        raise ValueError("backup verification requires aware time and positive age")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise RuntimeError("backup manifest schema is unsupported")
    created_at = datetime.fromisoformat(str(payload["created_at"]))
    if created_at.tzinfo is None or created_at > now:
        raise RuntimeError("backup manifest timestamp is invalid")
    if now - created_at > maximum_age:
        raise RuntimeError("verified backup is stale")
    backup = manifest_path.parent / str(payload["backup_file"])
    if not backup.is_file():
        raise RuntimeError("backup file is missing")
    if backup.stat().st_size != int(payload["size_bytes"]):
        raise RuntimeError("backup size does not match manifest")
    if _sha256(backup) != str(payload["sha256"]):
        raise RuntimeError("backup hash does not match manifest")
    store = SQLiteAccountingStore(backup)
    store.integrity_check()
    store.audit_ledger()
    if store.operational_fingerprint() != payload["database_fingerprint"]:
        raise RuntimeError("backup database fingerprint does not match manifest")
    return cast(Mapping[str, Any], payload)


@dataclass(frozen=True)
class ProductionHealth:
    status: str
    reasons: tuple[str, ...]
    metrics: Mapping[str, Any]


def production_health(
    store: SQLiteAccountingStore,
    *,
    now: datetime,
    maximum_reconciliation_age: timedelta,
    maximum_pending_outbox: int,
    backup_manifest: Path | None = None,
    maximum_backup_age: timedelta = timedelta(days=2),
) -> ProductionHealth:
    if now.tzinfo is None or maximum_reconciliation_age <= timedelta(0):
        raise ValueError("health thresholds are invalid")
    if maximum_pending_outbox < 0:
        raise ValueError("outbox threshold cannot be negative")
    reasons: list[str] = []
    store.integrity_check()
    ledger_count = store.audit_ledger()
    fingerprint = store.operational_fingerprint()
    latest = fingerprint["latest_reconciliation"]
    if latest is None:
        reasons.append("no reconciliation exists")
    else:
        reconciled_at = datetime.fromisoformat(str(latest["occurred_at"]))
        if latest["status"] != "PASSED":
            reasons.append("latest reconciliation is blocked")
        if reconciled_at > now or now - reconciled_at > maximum_reconciliation_age:
            reasons.append("latest reconciliation is stale")

    reporting = SQLiteReportingStore(store).operational_counts()
    outbox = reporting["outbox"]
    pending = int(outbox.get("PENDING", {}).get("count", 0)) + int(
        outbox.get("FAILED", {}).get("count", 0)
    )
    sending = int(outbox.get("SENDING", {}).get("count", 0))
    if pending > maximum_pending_outbox:
        reasons.append("email outbox backlog exceeds threshold")
    if sending:
        reasons.append("email delivery outcome requires manual review")
    if int(reporting["uninitialized_schedules"]):
        reasons.append("enabled report schedule is uninitialized")

    with store.read_connection() as connection:
        quarantined = int(
            connection.execute(
                "SELECT COUNT(*) FROM shadow_exchange_events WHERE status = 'QUARANTINED'"
            ).fetchone()[0]
        )
        unresolved_orders = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM owned_exchange_orders
                WHERE (is_algo = 0 AND exchange_order_id IS NULL)
                   OR (is_algo = 1 AND algo_id IS NULL)
                """
            ).fetchone()[0]
        )
    if quarantined:
        reasons.append("quarantined exchange events require review")
    if unresolved_orders:
        reasons.append("owned exchange orders remain unresolved")

    backup_ok = False
    if backup_manifest is None:
        reasons.append("verified backup is not configured")
    else:
        try:
            verify_backup_manifest(
                backup_manifest,
                now=now,
                maximum_age=maximum_backup_age,
            )
            backup_ok = True
        except Exception as exc:
            reasons.append(f"backup verification failed: {type(exc).__name__}")
    metrics = {
        "ledger_transactions": ledger_count,
        "pending_outbox": pending,
        "uncertain_outbox": sending,
        "quarantined_events": quarantined,
        "unresolved_orders": unresolved_orders,
        "enabled_schedules": reporting["enabled_schedules"],
        "uninitialized_schedules": reporting["uninitialized_schedules"],
        "backup_verified": backup_ok,
    }
    return ProductionHealth(
        status="HEALTHY" if not reasons else "BLOCKED",
        reasons=tuple(reasons),
        metrics=metrics,
    )
