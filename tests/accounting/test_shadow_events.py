from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from promptperp.accounting import (
    CashFlowGate,
    DuplicateEventConflict,
    ExchangeFunding,
    ExchangeTrade,
    InvestorAccountingService,
    ReportFrequency,
    ShadowAccountingSync,
)
from promptperp.domain import OrderSide, PositionSide
from promptperp.execution import TradeIntent
from promptperp.storage import SQLiteAccountingStore, SQLiteShadowEventStore

NOW = datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc)


class FakeReader:
    def __init__(self, *, trades=(), funding=(), actual_orders=None):
        self.trades = tuple(trades)
        self.funding = tuple(funding)
        self.actual_orders = dict(actual_orders or {})

    def list_trades(self, *, symbol, start_ms, end_ms):
        del start_ms, end_ms
        return tuple(item for item in self.trades if item.symbol == symbol)

    def list_funding(self, *, start_ms, end_ms):
        del start_ms, end_ms
        return self.funding

    def resolve_order(self, *, symbol, client_order_id):
        del symbol
        return self.actual_orders.get(client_order_id)

    def resolve_algo_order(self, *, symbol, client_order_id, algo_id):
        del symbol, client_order_id
        return algo_id, self.actual_orders.get(algo_id)


def configured_accounting(tmp_path):
    accounting_store = SQLiteAccountingStore(tmp_path / "accounting.sqlite3")
    service = InvestorAccountingService(accounting_store)
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
        strategy_id="sample_strategy",
        strategy_version="shadow-test",
        occurred_at=NOW,
    )
    service.contribute(
        event_id="cash-in-user0-shadow",
        investor_id="user0",
        pool_id="sample-pool",
        amount=Decimal("1500"),
        gate=CashFlowGate(
            checked_at=NOW,
            reconciled_at=NOW,
            reconciliation_id="shadow-bootstrap",
        ),
        actor="test-operator",
        reason="offline shadow fixture",
        external_reference="fake-shadow-transfer",
        occurred_at=NOW,
    )
    return service, accounting_store, SQLiteShadowEventStore(accounting_store)


def register_cycle(store, *, closed_at=NOW + timedelta(hours=2)):
    intent = TradeIntent(
        strategy_id="sample_strategy",
        run_id="run-shadow",
        intent_id="intent-shadow",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        quantity=Decimal("1"),
    )
    store.begin_intent(intent, opened_at=NOW)
    for role, client_id, order_id in (
        ("ENTRY", "bb-entry-test", "101"),
        ("EXIT", "bb-exit-test", "102"),
    ):
        store.prepare_order(
            intent_id=intent.intent_id,
            role=role,
            client_order_id=client_id,
            is_algo=False,
            created_at=NOW,
        )
        store.confirm_order(
            client_order_id=client_id,
            exchange_order_id=order_id,
        )
    store.close_intent(intent.intent_id, closed_at=closed_at)
    return intent


def test_ledger_posting_boundary_is_immutable(tmp_path):
    _, _, shadow_store = configured_accounting(tmp_path)

    first = shadow_store.ledger_posting_boundary(initialized_at=NOW)
    second = shadow_store.ledger_posting_boundary(
        initialized_at=NOW + timedelta(days=1)
    )

    assert first == NOW
    assert second == NOW


def test_first_ledger_posting_boundary_rejects_old_pending_events(tmp_path):
    _, _, shadow_store = configured_accounting(tmp_path)
    shadow_store.record_shadow_event(
        event_id="fictional-pre-cutover-event",
        event_type="TRADE",
        occurred_at=NOW - timedelta(minutes=1),
        payload={"symbol": "BTCUSDT"},
        status="VERIFIED_PENDING_LEDGER",
        reason="fictional old shadow event",
        observed_at=NOW,
    )

    with pytest.raises(ValueError, match="pre-cutover pending"):
        shadow_store.ledger_posting_boundary(initialized_at=NOW)


def test_verified_trades_and_funding_post_exactly_once(tmp_path):
    service, accounting_store, shadow_store = configured_accounting(tmp_path)
    register_cycle(shadow_store)
    reader = FakeReader(
        trades=(
            ExchangeTrade(
                symbol="BTCUSDT",
                trade_id="1",
                order_id="101",
                occurred_at=NOW + timedelta(minutes=1),
                realized_pnl=Decimal("0"),
                commission=Decimal("0.4"),
                commission_asset="USDT",
            ),
            ExchangeTrade(
                symbol="BTCUSDT",
                trade_id="2",
                order_id="102",
                occurred_at=NOW + timedelta(hours=1),
                realized_pnl=Decimal("20"),
                commission=Decimal("0.4"),
                commission_asset="USDT",
            ),
        ),
        funding=(
            ExchangeFunding(
                symbol="BTCUSDT",
                transaction_id="9001",
                occurred_at=NOW + timedelta(minutes=30),
                amount=Decimal("-1"),
                asset="USDT",
            ),
        ),
    )
    sync = ShadowAccountingSync(
        reader=reader,
        shadow_store=shadow_store,
        accounting_service=service,
        post_to_ledger=True,
    )

    first = sync.sync(start=NOW, end=NOW + timedelta(hours=2))
    second = sync.sync(start=NOW, end=NOW + timedelta(hours=2))

    assert first.posted_events == 3
    assert first.quarantined_events == 0
    assert second.duplicate_events == 3
    totals = shadow_store.settlement_totals("intent-shadow")
    assert totals.realized_pnl == Decimal("20")
    assert totals.commission == Decimal("0.8")
    assert totals.funding == Decimal("-1")
    assert totals.net_amount == Decimal("18.2")
    assert totals.event_count == 3
    with accounting_store.read_connection() as connection:
        assert accounting_store.pool_cash(connection, "sample-pool") == Decimal(
            "1518.2"
        )


def test_foreign_trade_is_quarantined_and_never_posted(tmp_path):
    service, accounting_store, shadow_store = configured_accounting(tmp_path)
    register_cycle(shadow_store)
    reader = FakeReader(
        trades=(
            ExchangeTrade(
                symbol="BTCUSDT",
                trade_id="foreign-1",
                order_id="999",
                occurred_at=NOW + timedelta(minutes=5),
                realized_pnl=Decimal("10"),
                commission=Decimal("0.1"),
                commission_asset="USDT",
            ),
        )
    )
    report = ShadowAccountingSync(
        reader=reader,
        shadow_store=shadow_store,
        accounting_service=service,
        post_to_ledger=True,
    ).sync(start=NOW, end=NOW + timedelta(hours=2))

    assert report.quarantined_events == 1
    assert shadow_store.event_status("binance:trade:BTCUSDT:foreign-1") == (
        "QUARANTINED"
    )
    totals = shadow_store.settlement_totals("intent-shadow")
    assert totals.event_count == 0
    assert totals.net_amount == 0
    with accounting_store.read_connection() as connection:
        assert accounting_store.pool_cash(connection, "sample-pool") == Decimal("1500")


def test_algo_identity_resolves_to_actual_order_before_trade_attribution(tmp_path):
    service, _, shadow_store = configured_accounting(tmp_path)
    intent = register_cycle(shadow_store)
    shadow_store.prepare_order(
        intent_id=intent.intent_id,
        role="STOP",
        client_order_id="bb-stop-test",
        is_algo=True,
        created_at=NOW,
    )
    shadow_store.confirm_order(
        client_order_id="bb-stop-test", exchange_order_id="algo-77"
    )
    reader = FakeReader(
        trades=(
            ExchangeTrade(
                symbol="BTCUSDT",
                trade_id="stop-fill",
                order_id="202",
                occurred_at=NOW + timedelta(minutes=10),
                realized_pnl=Decimal("-5"),
                commission=Decimal("0.2"),
                commission_asset="USDT",
            ),
        ),
        actual_orders={"algo-77": "202"},
    )

    report = ShadowAccountingSync(
        reader=reader,
        shadow_store=shadow_store,
        accounting_service=service,
        post_to_ledger=True,
    ).sync(start=NOW, end=NOW + timedelta(hours=2))

    assert report.posted_events == 1
    assert report.quarantined_events == 0


def test_unknown_normal_order_response_is_recovered_by_client_id(tmp_path):
    service, _, shadow_store = configured_accounting(tmp_path)
    intent = TradeIntent(
        strategy_id="sample_strategy",
        run_id="run-recovery",
        intent_id="intent-recovery",
        symbol="ETHUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        quantity=Decimal("1"),
    )
    shadow_store.begin_intent(intent, opened_at=NOW)
    shadow_store.prepare_order(
        intent_id=intent.intent_id,
        role="ENTRY",
        client_order_id="bb-entry-recovery",
        is_algo=False,
        created_at=NOW,
    )
    shadow_store.close_intent(intent.intent_id, closed_at=NOW + timedelta(hours=1))
    reader = FakeReader(
        trades=(
            ExchangeTrade(
                symbol="ETHUSDT",
                trade_id="recovered-fill",
                order_id="303",
                occurred_at=NOW + timedelta(minutes=1),
                realized_pnl=Decimal("0"),
                commission=Decimal("0.1"),
                commission_asset="USDT",
            ),
        ),
        actual_orders={"bb-entry-recovery": "303"},
    )

    report = ShadowAccountingSync(
        reader=reader,
        shadow_store=shadow_store,
        accounting_service=service,
        post_to_ledger=True,
    ).sync(start=NOW, end=NOW + timedelta(hours=1))

    assert report.posted_events == 1
    assert report.quarantined_events == 0


def test_capture_boundary_is_created_once_and_never_moves(tmp_path):
    _, _, shadow_store = configured_accounting(tmp_path)

    first = shadow_store.capture_boundary(initialized_at=NOW)
    second = shadow_store.capture_boundary(initialized_at=NOW + timedelta(days=1))

    assert first == NOW
    assert second == NOW


def test_ownership_ids_are_idempotent_but_conflicts_fail_closed(tmp_path):
    _, _, shadow_store = configured_accounting(tmp_path)
    intent = register_cycle(shadow_store)
    shadow_store.begin_intent(intent, opened_at=NOW)

    conflicting = TradeIntent(
        strategy_id=intent.strategy_id,
        run_id=intent.run_id,
        intent_id=intent.intent_id,
        symbol="ETHUSDT",
        side=intent.side,
        position_side=intent.position_side,
        quantity=intent.quantity,
    )
    with pytest.raises(DuplicateEventConflict, match="different ownership"):
        shadow_store.begin_intent(conflicting, opened_at=NOW)


def test_settlement_totals_reject_unknown_intent(tmp_path):
    _, _, shadow_store = configured_accounting(tmp_path)

    with pytest.raises(ValueError, match="unknown"):
        shadow_store.settlement_totals("missing-intent")
