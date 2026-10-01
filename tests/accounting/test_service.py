from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from promptperp.accounting import (
    AccountingValidationError,
    CashFlowBlocked,
    CashFlowGate,
    DuplicateEventConflict,
    InvestorAccountingService,
    LedgerIntegrityError,
    ReconciliationBlocked,
    ReconciliationStatus,
    ReportFrequency,
    SourceEvent,
    TradingSettlement,
)
from promptperp.storage import SQLiteAccountingStore

NOW = datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def accounting(tmp_path):
    store = SQLiteAccountingStore(tmp_path / "accounting.sqlite3")
    service = InvestorAccountingService(store)
    service.initialize()
    service.register_investor(
        investor_id="user0",
        display_name="Test User Zero",
        email="user0@example.invalid",
        frequency=ReportFrequency.DAILY,
        timezone_name="Asia/Singapore",
        local_send_time="09:00",
        occurred_at=NOW,
    )
    service.create_pool(
        pool_id="sample-pool",
        strategy_id="sample-strategy",
        strategy_version="test-v1",
        occurred_at=NOW,
    )
    return service, store


def flat_gate(reconciliation_id: str = "preflow-bootstrap") -> CashFlowGate:
    return CashFlowGate(
        checked_at=NOW,
        reconciled_at=NOW,
        reconciliation_id=reconciliation_id,
    )


def contribute_user0(service: InvestorAccountingService) -> Decimal:
    return service.contribute(
        event_id="cash-in-user0-001",
        investor_id="user0",
        pool_id="sample-pool",
        amount=Decimal("1500"),
        gate=flat_gate(),
        actor="test-operator",
        reason="offline acceptance fixture",
        external_reference="fake-transfer-001",
        occurred_at=NOW,
    )


def test_user0_1500_end_to_end_and_duplicate_is_idempotent(accounting):
    service, store = accounting

    assert contribute_user0(service) == Decimal("1500.000000000000000000")
    assert contribute_user0(service) == Decimal("1500.000000000000000000")

    reconciliation, snapshots = service.reconcile_and_value(
        reconciliation_id="recon-001",
        occurred_at=NOW,
        exchange_equity=Decimal("1500"),
        unrealized_pnl={"sample-pool": Decimal("0")},
    )
    assert reconciliation.status is ReconciliationStatus.PASSED
    assert len(snapshots) == 1

    view = service.investor_view("user0")
    assert view.equity == Decimal("1500.000000000000000000000000000000000000")
    assert view.net_contributions == Decimal("1500")
    assert view.return_rate == 0
    assert store.audit_ledger() == 1


def test_settlement_attributes_pnl_fee_and_funding_exactly_once(accounting):
    service, store = accounting
    contribute_user0(service)
    settlement = TradingSettlement(
        event_id="settlement-001",
        occurred_at=NOW,
        strategy_id="sample-strategy",
        run_id="run-test",
        intent_id="intent-test",
        order_id="order-test",
        trade_id="trade-test",
        realized_pnl=Decimal("100"),
        commission=Decimal("2"),
        funding=Decimal("-1"),
    )

    assert service.record_settlement(settlement) == Decimal("97")
    assert service.record_settlement(settlement) == Decimal("97")
    result, _ = service.reconcile_and_value(
        reconciliation_id="recon-after-settlement",
        occurred_at=NOW,
        exchange_equity=Decimal("1597"),
        unrealized_pnl={"sample-pool": Decimal("0")},
    )

    assert result.status is ReconciliationStatus.PASSED
    view = service.investor_view("user0")
    assert view.equity == Decimal("1597.000000000000000000000000000000000000")
    assert view.return_rate == Decimal("97") / Decimal("1500")
    assert store.audit_ledger() == 2


def test_multiple_investors_share_pool_by_units_without_nav_discontinuity(accounting):
    service, _ = accounting
    contribute_user0(service)
    service.record_settlement(
        TradingSettlement(
            event_id="settlement-profit-150",
            occurred_at=NOW,
            strategy_id="sample-strategy",
            run_id="run-test",
            intent_id="intent-profit",
            order_id="order-profit",
            trade_id="trade-profit",
            realized_pnl=Decimal("150"),
            commission=Decimal("0"),
            funding=Decimal("0"),
        )
    )
    service.reconcile_and_value(
        reconciliation_id="recon-nav-1.1",
        occurred_at=NOW,
        exchange_equity=Decimal("1650"),
        unrealized_pnl={"sample-pool": Decimal("0")},
    )
    service.register_investor(
        investor_id="user1",
        display_name="Test User One",
        email="user1@example.invalid",
        frequency=ReportFrequency.MONTHLY,
        timezone_name="Asia/Singapore",
        local_send_time="09:00",
        occurred_at=NOW,
    )

    issued = service.contribute(
        event_id="cash-in-user1-001",
        investor_id="user1",
        pool_id="sample-pool",
        amount=Decimal("1100"),
        gate=flat_gate("recon-nav-1.1"),
        actor="test-operator",
        reason="second offline investor",
        external_reference="fake-transfer-user1",
        occurred_at=NOW,
    )
    assert issued == Decimal("1000.000000000000000000")
    result, _ = service.reconcile_and_value(
        reconciliation_id="recon-two-investors",
        occurred_at=NOW,
        exchange_equity=Decimal("2750"),
        unrealized_pnl={"sample-pool": Decimal("0")},
    )
    assert result.status is ReconciliationStatus.PASSED
    assert service.investor_view("user0").equity == Decimal("1650")
    assert service.investor_view("user1").equity == Decimal("1100")

    redeemed = service.withdraw(
        event_id="cash-out-user1-001",
        investor_id="user1",
        amount=Decimal("110"),
        gate=flat_gate("recon-two-investors"),
        actor="test-operator",
        reason="offline partial withdrawal",
        external_reference="fake-withdrawal-user1",
        occurred_at=NOW,
    )
    assert redeemed == Decimal("100.000000000000000000")
    service.reconcile_and_value(
        reconciliation_id="recon-after-withdrawal",
        occurred_at=NOW,
        exchange_equity=Decimal("2640"),
        unrealized_pnl={"sample-pool": Decimal("0")},
    )
    assert service.investor_view("user0").equity == Decimal("1650")
    assert service.investor_view("user1").equity == Decimal("990")


def test_failed_reconciliation_does_not_publish_valuation(accounting):
    service, _ = accounting
    contribute_user0(service)

    result, snapshots = service.reconcile_and_value(
        reconciliation_id="recon-blocked",
        occurred_at=NOW,
        exchange_equity=Decimal("1499"),
        unrealized_pnl={"sample-pool": Decimal("0")},
    )

    assert result.status is ReconciliationStatus.BLOCKED
    assert snapshots == ()
    with pytest.raises(AccountingValidationError, match="no reconciled valuation"):
        service.investor_view("user0")


def test_cash_flow_gate_must_match_the_valuation_reconciliation(accounting):
    service, store = accounting
    contribute_user0(service)
    service.reconcile_and_value(
        reconciliation_id="recon-for-withdrawal",
        occurred_at=NOW,
        exchange_equity=Decimal("1500"),
        unrealized_pnl={"sample-pool": Decimal("0")},
    )

    with pytest.raises(ReconciliationBlocked, match="does not match"):
        service.withdraw(
            event_id="withdraw-wrong-reconciliation",
            investor_id="user0",
            amount=Decimal("100"),
            gate=flat_gate("different-reconciliation"),
            actor="test-operator",
            reason="must be rejected",
            external_reference="fake-withdrawal-rejected",
            occurred_at=NOW,
        )

    assert store.audit_ledger() == 1


def test_duplicate_event_id_with_changed_payload_is_blocked(accounting):
    service, _ = accounting
    contribute_user0(service)

    with pytest.raises(DuplicateEventConflict):
        service.contribute(
            event_id="cash-in-user0-001",
            investor_id="user0",
            pool_id="sample-pool",
            amount=Decimal("1499"),
            gate=flat_gate(),
            actor="test-operator",
            reason="offline acceptance fixture",
            external_reference="fake-transfer-001",
            occurred_at=NOW,
        )


def test_position_blocks_cash_flow_without_writing(accounting):
    service, store = accounting
    blocked = CashFlowGate(
        checked_at=NOW,
        reconciled_at=NOW,
        reconciliation_id="recon-blocked-by-position",
        has_managed_position=True,
    )

    with pytest.raises(CashFlowBlocked):
        service.contribute(
            event_id="blocked-cashflow",
            investor_id="user0",
            pool_id="sample-pool",
            amount=Decimal("1500"),
            gate=blocked,
            actor="test-operator",
            reason="must not be accepted",
            external_reference="fake-transfer-blocked",
            occurred_at=NOW,
        )

    assert store.audit_ledger() == 0


def test_source_event_and_ledger_write_roll_back_together(accounting):
    _, store = accounting
    event = SourceEvent(
        event_id="rollback-event",
        event_type="TEST",
        occurred_at=NOW,
        payload={"value": "test"},
    )

    with pytest.raises(RuntimeError, match="injected"):
        with store.transaction() as connection:
            store.insert_source_event(connection, event)
            raise RuntimeError("injected failure")

    assert store.event_result(event) is None


def test_tampering_is_detected_and_online_backup_recovers(accounting, tmp_path):
    service, store = accounting
    contribute_user0(service)
    backup = tmp_path / "backup" / "accounting.sqlite3"
    store.backup(backup)
    recovered = SQLiteAccountingStore(backup)
    recovered.integrity_check()
    assert recovered.audit_ledger() == 1

    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            "UPDATE ledger_entries SET amount = '999' WHERE posting_index = 0"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(LedgerIntegrityError, match="modified"):
        store.audit_ledger()


def test_source_event_tampering_cannot_be_hidden_by_rehashing_event(accounting):
    service, store = accounting
    contribute_user0(service)
    changed_payload = json.dumps(
        {
            "actor": "attacker",
            "amount": "9999",
            "external_reference": "fake-transfer-001",
            "investor_id": "user0",
            "pool_id": "sample-pool",
            "reason": "offline acceptance fixture",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    envelope = json.dumps(
        {
            "event_id": "cash-in-user0-001",
            "event_type": "CONTRIBUTION",
            "schema_version": 1,
            "occurred_at": NOW.isoformat(),
            "payload": json.loads(changed_payload),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            "UPDATE source_events SET payload_json = ?, payload_hash = ? WHERE event_id = ?",
            (
                changed_payload,
                hashlib.sha256(envelope.encode()).hexdigest(),
                "cash-in-user0-001",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(LedgerIntegrityError, match="modified"):
        store.audit_ledger()
