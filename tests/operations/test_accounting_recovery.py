from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from promptperp.accounting import (
    CashFlowGate,
    InvestorAccountingService,
    ReportFrequency,
)
from promptperp.operations.accounting_recovery import (
    create_verified_backup,
    production_health,
    verify_backup_manifest,
)
from promptperp.reporting import next_delivery
from promptperp.storage import SQLiteAccountingStore
from promptperp.storage.reporting_sqlite import SQLiteReportingStore

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def prepared_store(tmp_path) -> SQLiteAccountingStore:
    store = SQLiteAccountingStore(tmp_path / "accounting.sqlite3")
    accounting = InvestorAccountingService(store)
    accounting.initialize()
    accounting.register_investor(
        investor_id="user0",
        display_name="Test User",
        email="user0@example.invalid",
        frequency=ReportFrequency.DAILY,
        timezone_name="Asia/Singapore",
        local_send_time="15:00",
        occurred_at=NOW,
    )
    accounting.create_pool(
        pool_id="pool-1",
        strategy_id="strategy-1",
        strategy_version="v1",
        occurred_at=NOW,
    )
    accounting.contribute(
        event_id="opening",
        investor_id="user0",
        pool_id="pool-1",
        amount=Decimal("100"),
        gate=CashFlowGate(
            checked_at=NOW,
            reconciled_at=NOW,
            reconciliation_id="opening-reconciliation",
        ),
        actor="test",
        reason="offline fixture",
        external_reference="offline-opening",
        occurred_at=NOW,
    )
    accounting.reconcile_and_value(
        reconciliation_id="reconciliation-1",
        occurred_at=NOW,
        exchange_equity=Decimal("100"),
        unrealized_pnl={"pool-1": Decimal("0")},
    )
    due = next_delivery(
        ReportFrequency.DAILY,
        after=NOW,
        timezone_name="Asia/Singapore",
        local_send_time="15:00",
    )
    SQLiteReportingStore(store).initialize_schedule(
        investor_id="user0", next_due_at=due, occurred_at=NOW
    )
    return store


def test_online_backup_has_hash_manifest_and_restores_exact_fingerprint(tmp_path):
    store = prepared_store(tmp_path)
    backup = tmp_path / "backup" / "accounting.sqlite3"

    manifest = create_verified_backup(store, destination=backup, occurred_at=NOW)
    payload = verify_backup_manifest(
        manifest,
        now=NOW + timedelta(hours=1),
        maximum_age=timedelta(days=1),
    )

    assert payload["database_fingerprint"] == store.operational_fingerprint()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert manifest.stat().st_mode & 0o777 == 0o600


def test_backup_tampering_and_staleness_fail_closed(tmp_path):
    store = prepared_store(tmp_path)
    backup = tmp_path / "backup.sqlite3"
    manifest = create_verified_backup(store, destination=backup, occurred_at=NOW)

    with pytest.raises(RuntimeError, match="stale"):
        verify_backup_manifest(
            manifest,
            now=NOW + timedelta(days=2),
            maximum_age=timedelta(days=1),
        )
    with backup.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(RuntimeError, match="size"):
        verify_backup_manifest(
            manifest,
            now=NOW,
            maximum_age=timedelta(days=1),
        )


def test_production_health_covers_reconciliation_outbox_shadow_and_backup(tmp_path):
    store = prepared_store(tmp_path)
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO execution_intents(
                intent_id, strategy_id, run_id, symbol, opened_at
            ) VALUES ('intent-1', 'strategy-1', 'run-1', 'TESTUSDT', ?)
            """,
            (NOW.isoformat(),),
        )
        connection.execute(
            """
            INSERT INTO owned_exchange_orders(
                client_order_id, intent_id, role, is_algo, algo_id, created_at
            ) VALUES ('algo-stop-1', 'intent-1', 'STOP', 1, 'algo-1', ?)
            """,
            (NOW.isoformat(),),
        )
    manifest = create_verified_backup(
        store,
        destination=tmp_path / "backup.sqlite3",
        occurred_at=NOW,
    )

    healthy = production_health(
        store,
        now=NOW + timedelta(minutes=1),
        maximum_reconciliation_age=timedelta(minutes=15),
        maximum_pending_outbox=0,
        backup_manifest=manifest,
        maximum_backup_age=timedelta(days=1),
    )
    stale = production_health(
        store,
        now=NOW + timedelta(minutes=16),
        maximum_reconciliation_age=timedelta(minutes=15),
        maximum_pending_outbox=0,
        backup_manifest=manifest,
        maximum_backup_age=timedelta(days=1),
    )

    assert healthy.status == "HEALTHY"
    assert healthy.reasons == ()
    assert stale.status == "BLOCKED"
    assert "latest reconciliation is stale" in stale.reasons
