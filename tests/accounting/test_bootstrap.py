from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from importlib import resources

import pytest

from promptperp.accounting import (
    AccountingValidationError,
    CashFlowGate,
    InvestorAccountingService,
    ReportFrequency,
    TradingSettlement,
)
from promptperp.accounting.bootstrap import (
    ImportedInvestorSnapshot,
    OpeningInvestor,
    SharedPoolBootstrap,
    bootstrap_shared_pool,
)
from promptperp.storage import SQLiteAccountingStore

NOW = datetime(2030, 1, 31, 7, 0, tzinfo=timezone.utc)


def opening_spec() -> SharedPoolBootstrap:
    return SharedPoolBootstrap(
        migration_id="fictional-opening",
        pool_id="fictional-pool",
        strategy_id="fictional-strategy",
        strategy_version="v1",
        occurred_at=NOW,
        exchange_equity=Decimal("2400.00000000"),
        investors=(
            OpeningInvestor(
                investor_id="fictional-owner",
                display_name="Fictional Owner",
                email="owner@example.invalid",
                opening_contribution=Decimal("1500.00000000"),
                opening_units=Decimal("1500.000000000000000000"),
            ),
            OpeningInvestor(
                investor_id="fictional-client",
                display_name="Fictional Client",
                email="client@example.invalid",
                opening_contribution=Decimal("600.00000000"),
                opening_units=Decimal("600.000000000000000000"),
            ),
        ),
        imported_snapshots=(
            ImportedInvestorSnapshot(
                record_id="fictional-history-01",
                investor_id="fictional-client",
                occurred_at=datetime(2030, 1, 24, 7, 0, tzinfo=timezone.utc),
                units=Decimal("600.000000000000000000"),
                equity=Decimal("720.00000000"),
                source_note="fictional operator-attested report",
            ),
        ),
    )


def test_shared_pool_bootstrap_is_balanced_auditable_and_idempotent(tmp_path):
    store = SQLiteAccountingStore(tmp_path / "accounting.sqlite3")
    store.initialize()

    first = bootstrap_shared_pool(store, opening_spec())
    second = bootstrap_shared_pool(store, opening_spec())

    assert first == second
    assert first.pool_equity == Decimal("2400.00000000")
    assert first.outstanding_units == Decimal("2100.000000000000000000")
    with store.read_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM investors").fetchone()[0] == 2
        assert (
            connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM imported_investor_snapshots"
            ).fetchone()[0]
            == 1
        )
        assert store.pool_cash(connection, "fictional-pool") == Decimal("2400.00000000")
    assert store.audit_ledger() == 1


def test_schema_v2_upgrades_to_multi_pool_without_losing_subscription(tmp_path):
    path = tmp_path / "accounting.sqlite3"
    connection = sqlite3.connect(path)
    migrations = resources.files("promptperp.storage.migrations")
    connection.executescript(
        migrations.joinpath("0001_accounting.sql").read_text(encoding="utf-8")
    )
    connection.executescript(
        migrations.joinpath("0002_shadow_events.sql").read_text(encoding="utf-8")
    )
    connection.executemany(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        ((1, NOW.isoformat()), (2, NOW.isoformat())),
    )
    connection.execute(
        "INSERT INTO investors(investor_id, display_name, created_at) VALUES ('legacy', 'Legacy', ?)",
        (NOW.isoformat(),),
    )
    connection.execute(
        """
        INSERT INTO strategy_pools(
            pool_id, strategy_id, strategy_version, base_asset,
            initial_unit_nav, created_at
        ) VALUES ('legacy-pool', 'legacy-strategy', 'v1', 'USDT', '1', ?)
        """,
        (NOW.isoformat(),),
    )
    connection.execute(
        """
        INSERT INTO subscriptions(
            investor_id, pool_id, units, total_contributions,
            total_withdrawals, active, updated_at
        ) VALUES ('legacy', 'legacy-pool', '10', '10', '0', 1, ?)
        """,
        (NOW.isoformat(),),
    )
    connection.commit()
    connection.close()

    store = SQLiteAccountingStore(path)
    store.initialize()
    store.initialize()

    with store.read_connection() as upgraded:
        assert (
            upgraded.execute(
                "SELECT units FROM subscriptions WHERE investor_id = 'legacy' AND pool_id = 'legacy-pool'"
            ).fetchone()[0]
            == "10"
        )
        assert (
            upgraded.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == 5
        )
        assert (
            upgraded.execute(
                "SELECT COUNT(*) FROM imported_investor_snapshots"
            ).fetchone()[0]
            == 0
        )


def test_bootstrap_refuses_nonempty_business_ledger(tmp_path):
    store = SQLiteAccountingStore(tmp_path / "accounting.sqlite3")
    service = InvestorAccountingService(store)
    service.initialize()
    service.register_investor(
        investor_id="existing",
        display_name="Existing",
        email="existing@example.invalid",
        frequency=ReportFrequency.MONTHLY,
        timezone_name="Asia/Singapore",
        local_send_time="15:00",
        occurred_at=NOW,
    )

    with pytest.raises(AccountingValidationError, match="empty business ledger"):
        bootstrap_shared_pool(store, opening_spec())


def test_investor_can_subscribe_to_multiple_strategy_pools(tmp_path):
    store = SQLiteAccountingStore(tmp_path / "accounting.sqlite3")
    service = InvestorAccountingService(store)
    service.initialize()
    service.register_investor(
        investor_id="multi-user",
        display_name="Multi User",
        email="multi@example.invalid",
        frequency=ReportFrequency.MONTHLY,
        timezone_name="Asia/Singapore",
        local_send_time="15:00",
        monthly_send_day=24,
        occurred_at=NOW,
    )
    for pool_id in ("pool-a", "pool-b"):
        service.create_pool(
            pool_id=pool_id,
            strategy_id=f"strategy-{pool_id}",
            strategy_version="v1",
            occurred_at=NOW,
        )
        service.contribute(
            event_id=f"contribution-{pool_id}",
            investor_id="multi-user",
            pool_id=pool_id,
            amount=Decimal("1000"),
            gate=CashFlowGate(
                checked_at=NOW,
                reconciled_at=NOW,
                reconciliation_id="bootstrap",
            ),
            actor="test-operator",
            reason="fictional multi-pool contribution",
            external_reference=f"fictional-{pool_id}",
            occurred_at=NOW,
        )

    with store.read_connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE investor_id = 'multi-user'"
            ).fetchone()[0]
            == 2
        )

    service.reconcile_and_value(
        reconciliation_id="fictional-multi-pool-reconciliation",
        occurred_at=NOW,
        exchange_equity=Decimal("2000"),
        unrealized_pnl={"pool-a": Decimal("0"), "pool-b": Decimal("0")},
    )
    gate = CashFlowGate(
        checked_at=NOW,
        reconciled_at=NOW,
        reconciliation_id="fictional-multi-pool-reconciliation",
    )
    withdrawal = {
        "investor_id": "multi-user",
        "amount": Decimal("100"),
        "gate": gate,
        "actor": "test-operator",
        "reason": "fictional pool-specific withdrawal",
        "external_reference": "fictional-withdrawal-reference",
        "occurred_at": NOW,
    }
    with pytest.raises(AccountingValidationError, match="pool_id is required"):
        service.withdraw(event_id="ambiguous-withdrawal", **withdrawal)

    redeemed = service.withdraw(
        event_id="pool-a-withdrawal",
        pool_id="pool-a",
        **withdrawal,
    )

    assert redeemed == Decimal("100.000000000000000000")
    with store.read_connection() as connection:
        balances = {
            row["pool_id"]: Decimal(row["units"])
            for row in connection.execute(
                "SELECT pool_id, units FROM subscriptions WHERE investor_id = 'multi-user'"
            )
        }
    assert balances == {
        "pool-a": Decimal("900.000000000000000000"),
        "pool-b": Decimal("1000.000000000000000000"),
    }


def test_latest_snapshot_orders_mixed_timezone_offsets_by_instant(tmp_path):
    store = SQLiteAccountingStore(tmp_path / "accounting.sqlite3")
    store.initialize()
    opening_time = datetime.fromisoformat("2030-01-02T02:00:00+08:00")
    later_utc = datetime(2030, 1, 1, 18, 30, tzinfo=timezone.utc)
    spec = replace(
        opening_spec(),
        occurred_at=opening_time,
        imported_snapshots=(),
    )
    bootstrap_shared_pool(store, spec)
    service = InvestorAccountingService(store)
    service.record_settlement(
        TradingSettlement(
            event_id="fictional-timezone-settlement",
            occurred_at=later_utc,
            strategy_id="fictional-strategy",
            run_id="fictional-timezone-run",
            intent_id="fictional-timezone-intent",
            order_id="fictional-timezone-order",
            trade_id="fictional-timezone-trade",
            realized_pnl=Decimal("100"),
            commission=Decimal("0"),
            funding=Decimal("0"),
        )
    )
    service.reconcile_and_value(
        reconciliation_id="fictional-timezone-reconciliation",
        occurred_at=later_utc,
        exchange_equity=Decimal("2500"),
        unrealized_pnl={"fictional-pool": Decimal("0")},
    )

    view = service.investor_view("fictional-owner")

    assert view.snapshot_at == later_utc
    assert view.equity == Decimal("1785.71428571")
