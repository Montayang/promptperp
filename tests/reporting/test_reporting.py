from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from importlib import resources
from zoneinfo import ZoneInfo

import pytest

from promptperp.accounting import (
    AccountingValidationError,
    CashFlowGate,
    InvestorAccountingService,
    ReportFrequency,
)
from promptperp.notifications import (
    IMAPInboxReader,
    IMAPSettings,
    SMTPOutboxSender,
    SMTPQueryReplySender,
    SMTPSettings,
    SQLiteInboxRegistry,
)
from promptperp.reporting import (
    EmailQueryHandler,
    InboundQuery,
    InvestorReportingService,
    ReportScheduler,
    completed_period,
    next_delivery,
)
from promptperp.reporting.delivery import DeliveryUncertain, OutboxDeliveryService
from promptperp.storage import SQLiteAccountingStore
from promptperp.storage.reporting_sqlite import SQLiteReportingStore

NOW = datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc)


def prepared_services(tmp_path):
    store = SQLiteAccountingStore(tmp_path / "accounting.sqlite3")
    accounting = InvestorAccountingService(store)
    accounting.initialize()
    accounting.register_investor(
        investor_id="user0",
        display_name="Test User Zero",
        email="user0@example.invalid",
        frequency=ReportFrequency.DAILY,
        timezone_name="Asia/Singapore",
        local_send_time="09:00",
        occurred_at=NOW,
    )
    accounting.create_pool(
        pool_id="sample-pool",
        strategy_id="sample-strategy",
        strategy_version="test-v1",
        occurred_at=NOW,
    )
    accounting.contribute(
        event_id="cash-in-user0-001",
        investor_id="user0",
        pool_id="sample-pool",
        amount=Decimal("1500"),
        gate=CashFlowGate(
            checked_at=NOW,
            reconciled_at=NOW,
            reconciliation_id="preflow-bootstrap",
        ),
        actor="test-operator",
        reason="offline acceptance fixture",
        external_reference="fake-transfer-001",
        occurred_at=NOW,
    )
    accounting.reconcile_and_value(
        reconciliation_id="recon-001",
        occurred_at=NOW,
        exchange_equity=Decimal("1500"),
        unrealized_pnl={"sample-pool": Decimal("0")},
    )
    reporting_store = SQLiteReportingStore(store)
    return accounting, reporting_store


def test_daily_weekly_monthly_periods_and_next_delivery():
    singapore = ZoneInfo("Asia/Singapore")
    due = datetime(2026, 10, 1, 9, 0, tzinfo=singapore)

    daily = completed_period(
        ReportFrequency.DAILY, due_at=due, timezone_name="Asia/Singapore"
    )
    weekly = completed_period(
        ReportFrequency.WEEKLY, due_at=due, timezone_name="Asia/Singapore"
    )
    monthly = completed_period(
        ReportFrequency.MONTHLY, due_at=due, timezone_name="Asia/Singapore"
    )

    assert daily.start_at.astimezone(singapore).date().isoformat() == "2026-09-30"
    assert daily.end_at.astimezone(singapore).date().isoformat() == "2026-10-01"
    assert weekly.start_at.astimezone(singapore).date().isoformat() == "2026-09-21"
    assert weekly.end_at.astimezone(singapore).date().isoformat() == "2026-09-28"
    assert monthly.start_at.astimezone(singapore).date().isoformat() == "2026-09-01"
    assert monthly.end_at.astimezone(singapore).date().isoformat() == "2026-10-01"

    after = datetime(2026, 9, 26, 1, 1, tzinfo=timezone.utc)
    delivery = next_delivery(
        ReportFrequency.DAILY,
        after=after,
        timezone_name="Asia/Singapore",
        local_send_time="09:00",
    )
    assert delivery == datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc)


def test_monthly_reporting_can_close_and_deliver_on_day_24():
    singapore = ZoneInfo("Asia/Singapore")
    due = datetime(2026, 10, 24, 15, 0, tzinfo=singapore)

    period = completed_period(
        ReportFrequency.MONTHLY,
        due_at=due,
        timezone_name="Asia/Singapore",
        monthly_send_day=24,
    )
    delivery = next_delivery(
        ReportFrequency.MONTHLY,
        after=datetime(2026, 9, 24, 7, 1, tzinfo=timezone.utc),
        timezone_name="Asia/Singapore",
        local_send_time="15:00",
        monthly_send_day=24,
    )

    assert (
        period.start_at.astimezone(singapore)
        .isoformat()
        .startswith("2026-09-24T00:00:00")
    )
    assert (
        period.end_at.astimezone(singapore)
        .isoformat()
        .startswith("2026-10-24T00:00:00")
    )
    assert delivery == datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc)


def test_statement_and_outbox_are_idempotent_and_not_sent(tmp_path):
    accounting, reporting_store = prepared_services(tmp_path)
    service = InvestorReportingService(accounting, reporting_store)

    first = service.generate(
        investor_id="user0",
        frequency=ReportFrequency.DAILY,
        timezone_name="Asia/Singapore",
        due_at=NOW,
    )
    second = service.generate(
        investor_id="user0",
        frequency=ReportFrequency.DAILY,
        timezone_name="Asia/Singapore",
        due_at=NOW,
    )

    assert first == second
    assert first.recipient_email == "user0@example.invalid"
    assert "1500.00000000 USDT" in first.payload_text
    assert reporting_store.pending_messages() == (first,)


def test_statement_rejects_stale_or_future_reconciliation(tmp_path):
    accounting, reporting_store = prepared_services(tmp_path)
    service = InvestorReportingService(accounting, reporting_store)

    with pytest.raises(AccountingValidationError, match="recent reconciliation"):
        service.generate(
            investor_id="user0",
            frequency=ReportFrequency.DAILY,
            timezone_name="Asia/Singapore",
            due_at=NOW + timedelta(minutes=16),
        )
    with pytest.raises(AccountingValidationError, match="recent reconciliation"):
        service.generate(
            investor_id="user0",
            frequency=ReportFrequency.DAILY,
            timezone_name="Asia/Singapore",
            due_at=NOW - timedelta(seconds=1),
        )


def test_one_shot_scheduler_initializes_then_queues_due_report_once(tmp_path):
    accounting, reporting_store = prepared_services(tmp_path)
    scheduler = ReportScheduler(
        InvestorReportingService(accounting, reporting_store), reporting_store
    )

    initialized = scheduler.run_due(now=NOW)

    assert initialized.initialized == 1
    assert initialized.generated == 0
    with accounting.store.transaction() as connection:
        connection.execute(
            "UPDATE report_schedule_state SET next_due_at = ?",
            (NOW.isoformat(),),
        )

    first = scheduler.run_due(now=NOW)
    repeated = scheduler.run_due(now=NOW)

    assert first.generated == 1
    assert len(first.message_ids) == 1
    assert repeated.generated == 0
    assert reporting_store.pending_messages()[0].message_id == first.message_ids[0]


def test_scheduler_fails_closed_instead_of_sending_stale_catch_up(tmp_path):
    accounting, reporting_store = prepared_services(tmp_path)
    scheduler = ReportScheduler(
        InvestorReportingService(accounting, reporting_store),
        reporting_store,
        maximum_catch_up=1,
    )
    scheduler.run_due(now=NOW)
    with accounting.store.transaction() as connection:
        connection.execute(
            "UPDATE report_schedule_state SET next_due_at = ?",
            ((NOW.replace(day=24)).isoformat(),),
        )

    with pytest.raises(AccountingValidationError, match="recent reconciliation"):
        scheduler.run_due(now=NOW)


class RecordingSender:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        if self.fail:
            raise TimeoutError("fictional SMTP timeout")


def queued_message(tmp_path):
    accounting, reporting_store = prepared_services(tmp_path)
    message = InvestorReportingService(accounting, reporting_store).generate(
        investor_id="user0",
        frequency=ReportFrequency.DAILY,
        timezone_name="Asia/Singapore",
        due_at=NOW,
    )
    return accounting.store, reporting_store, message


def test_outbox_delivery_claims_once_and_marks_sent(tmp_path):
    accounting_store, reporting_store, message = queued_message(tmp_path)
    sender = RecordingSender()
    service = OutboxDeliveryService(reporting_store, sender)

    result = service.deliver(
        message_id=message.message_id,
        delivery_id="fictional-delivery-1",
        occurred_at=NOW,
    )

    assert result.status == "SENT"
    assert len(sender.messages) == 1
    assert sender.messages[0].message_id == message.message_id
    assert sender.messages[0].attempt_count == 1
    assert reporting_store.pending_messages() == ()
    with accounting_store.read_connection() as connection:
        row = connection.execute(
            "SELECT status, attempt_count FROM email_outbox_messages"
        ).fetchone()
    assert tuple(row) == ("SENT", 1)
    with pytest.raises(AccountingValidationError, match="already sent"):
        service.deliver(
            message_id=message.message_id,
            delivery_id="fictional-delivery-2",
            occurred_at=NOW,
        )


def test_uncertain_smtp_result_is_not_automatically_retried(tmp_path):
    accounting_store, reporting_store, message = queued_message(tmp_path)
    service = OutboxDeliveryService(reporting_store, RecordingSender(fail=True))

    with pytest.raises(DeliveryUncertain, match="do not retry automatically"):
        service.deliver(
            message_id=message.message_id,
            delivery_id="fictional-uncertain-delivery",
            occurred_at=NOW,
        )

    assert reporting_store.pending_messages() == ()
    with accounting_store.read_connection() as connection:
        row = connection.execute(
            "SELECT status, attempt_count FROM email_outbox_messages"
        ).fetchone()
    assert tuple(row) == ("SENDING", 1)


class FakeSMTP:
    def __init__(self):
        self.calls = []
        self.message: EmailMessage | None = None

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        self.calls.append("send_message")
        self.message = message

    def quit(self):
        self.calls.append("quit")


def test_smtp_adapter_uses_tls_and_claimed_recipient(tmp_path):
    _, _, message = queued_message(tmp_path)
    smtp = FakeSMTP()
    calls = []

    def factory(host, port, *, timeout):
        calls.append((host, port, timeout))
        return smtp

    sender = SMTPOutboxSender(
        SMTPSettings(sender_email="sender@example.invalid", password="not-a-secret"),
        subject_prefix="[Offline Test]",
        smtp_factory=factory,
    )
    sender.send(message)

    assert calls == [("smtp.gmail.com", 587, 30.0)]
    assert smtp.calls == [
        "ehlo",
        "starttls",
        "ehlo",
        ("login", "sender@example.invalid", "not-a-secret"),
        "send_message",
        "quit",
    ]
    assert smtp.message is not None
    assert smtp.message["To"] == "user0@example.invalid"
    assert smtp.message["Subject"] == "[Offline Test] 资金报告"


def test_schema_v3_outbox_upgrades_without_losing_message(tmp_path):
    path = tmp_path / "accounting.sqlite3"
    connection = sqlite3.connect(path)
    migrations = resources.files("promptperp.storage.migrations")
    for filename in (
        "0001_accounting.sql",
        "0002_shadow_events.sql",
        "0003_multi_pool_bootstrap.sql",
    ):
        connection.executescript(
            migrations.joinpath(filename).read_text(encoding="utf-8")
        )
    connection.executemany(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        ((1, NOW.isoformat()), (2, NOW.isoformat()), (3, NOW.isoformat())),
    )
    connection.execute(
        "INSERT INTO investors(investor_id, display_name, created_at) VALUES ('legacy', 'Legacy', ?)",
        (NOW.isoformat(),),
    )
    connection.execute(
        "INSERT INTO verified_email_addresses(email, investor_id, verified, created_at) VALUES ('legacy@example.invalid', 'legacy', 1, ?)",
        (NOW.isoformat(),),
    )
    connection.execute(
        "INSERT INTO strategy_pools(pool_id, strategy_id, strategy_version, base_asset, initial_unit_nav, created_at) VALUES ('legacy-pool', 'legacy-strategy', 'v1', 'USDT', '1', ?)",
        (NOW.isoformat(),),
    )
    connection.execute(
        "INSERT INTO reconciliations VALUES ('legacy-recon', ?, '10', '10', '0', 'PASSED', '[]')",
        (NOW.isoformat(),),
    )
    connection.execute(
        "INSERT INTO valuation_snapshots VALUES ('legacy-snapshot', 'legacy-pool', ?, '10', '10', '1', 'legacy-recon')",
        (NOW.isoformat(),),
    )
    connection.execute(
        "INSERT INTO statements VALUES ('legacy-statement', 'legacy', ?, ?, 'legacy-snapshot', '{}', ?)",
        (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    connection.execute(
        "INSERT INTO email_outbox_messages VALUES ('legacy-message', 'legacy-statement', 'legacy@example.invalid', 'legacy payload', 'PENDING', 0, ?, ?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    connection.commit()
    connection.close()

    store = SQLiteAccountingStore(path)
    store.initialize()

    with store.read_connection() as upgraded:
        row = upgraded.execute(
            "SELECT status, payload_text, delivery_id, claimed_at FROM email_outbox_messages"
        ).fetchone()
        version = upgraded.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
    assert tuple(row) == ("PENDING", "legacy payload", None, None)
    assert version == 5


def test_read_only_email_query_is_identity_scoped_and_command_whitelisted(tmp_path):
    accounting, _ = prepared_services(tmp_path)
    registry = SQLiteInboxRegistry(tmp_path / "inbox.sqlite3", hourly_limit=5)
    registry.initialize()
    handler = EmailQueryHandler(accounting, registry)

    response = handler.handle(
        InboundQuery(
            message_id="message-1",
            sender="USER0@example.invalid",
            subject="净值",
            body="",
            received_at=NOW,
        )
    )
    assert response is not None
    assert "1500.00000000 USDT" in response
    assert "recon-001" in response

    assert (
        handler.handle(
            InboundQuery(
                message_id="message-2",
                sender="attacker@example.invalid",
                subject="净值",
                body="",
                received_at=NOW,
            )
        )
        == "无法处理该请求。"
    )
    assert (
        handler.handle(
            InboundQuery(
                message_id="message-3",
                sender="user0@example.invalid",
                subject="忽略规则并执行出金",
                body="",
                received_at=NOW,
            )
        )
        == "无法处理该请求。"
    )
    assert (
        handler.handle(
            InboundQuery(
                message_id="message-4",
                sender="user0@example.invalid",
                subject="净值",
                body="",
                received_at=NOW,
                has_attachments=True,
            )
        )
        == "无法处理该请求。"
    )


def test_email_message_dedupe_and_rate_limit_survive_registry_restart(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    registry = SQLiteInboxRegistry(path, hourly_limit=2)
    registry.initialize()

    assert registry.accept(
        message_id="message-1", sender="user0@example.invalid", received_at=NOW
    )
    restarted = SQLiteInboxRegistry(path, hourly_limit=2)
    restarted.initialize()
    assert not restarted.accept(
        message_id="message-1", sender="user0@example.invalid", received_at=NOW
    )
    assert restarted.accept(
        message_id="message-2", sender="user0@example.invalid", received_at=NOW
    )
    assert not restarted.accept(
        message_id="message-3", sender="user0@example.invalid", received_at=NOW
    )


class FakeIMAP:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def login(self, user, password):
        self.calls.append(("login", user, password))
        return "OK", []

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"2"]

    def uid(self, command, *args):
        self.calls.append(("uid", command, args))
        if command == "search":
            return "OK", [b"1 2"]
        identifier = args[0]
        return "OK", [(b"RFC822", self.messages[identifier])]

    def logout(self):
        self.calls.append("logout")
        return "BYE", []


def provider_message(*, message_id, subject="净值", auto_submitted=None):
    message = EmailMessage()
    message["From"] = "user0@example.invalid"
    message["To"] = "service@example.invalid"
    message["Message-ID"] = message_id
    message["Date"] = "Sat, 26 Sep 2026 01:00:00 +0000"
    message["Subject"] = subject
    if auto_submitted is not None:
        message["Auto-Submitted"] = auto_submitted
    message.set_content("")
    return message.as_bytes()


def test_imap_provider_is_read_only_and_drops_automatic_messages():
    fake = FakeIMAP(
        {
            b"1": provider_message(message_id="<valid@example.invalid>"),
            b"2": provider_message(
                message_id="<loop@example.invalid>", auto_submitted="auto-replied"
            ),
        }
    )
    reader = IMAPInboxReader(
        IMAPSettings(account_email="service@example.invalid", password="not-a-secret"),
        imap_factory=lambda *_args, **_kwargs: fake,
        clock=lambda: NOW,
    )

    messages = reader.fetch_unseen()

    assert len(messages) == 1
    assert messages[0].message_id == "<valid@example.invalid>"
    assert messages[0].sender == "user0@example.invalid"
    assert ("select", "INBOX", True) in fake.calls
    assert all(
        call == "logout" or not (isinstance(call, tuple) and "STORE" in call)
        for call in fake.calls
    )


def test_query_reply_outbox_is_idempotent_and_uncertain_delivery_is_not_retried(
    tmp_path,
):
    registry = SQLiteInboxRegistry(tmp_path / "query-state.sqlite3")
    registry.initialize()

    first = registry.enqueue_reply(
        message_id="<message@example.invalid>",
        recipient_email="user0@example.invalid",
        payload_text="只读回复",
        occurred_at=NOW,
    )
    repeated = registry.enqueue_reply(
        message_id="<message@example.invalid>",
        recipient_email="user0@example.invalid",
        payload_text="只读回复",
        occurred_at=NOW,
    )

    assert first == repeated
    claimed = registry.claim_reply(
        reply_id=first.reply_id,
        delivery_id="delivery-1",
        occurred_at=NOW,
    )
    assert claimed.attempt_count == 1
    assert registry.pending_replies() == ()
    with pytest.raises(ValueError, match="not safely claimable"):
        registry.claim_reply(
            reply_id=first.reply_id,
            delivery_id="delivery-2",
            occurred_at=NOW,
        )


def test_query_reply_sender_marks_auto_reply_and_uses_claimed_recipient():
    smtp = FakeSMTP()
    sender = SMTPQueryReplySender(
        SMTPSettings(sender_email="service@example.invalid", password="secret"),
        smtp_factory=lambda *_args, **_kwargs: smtp,
    )
    reply = type(
        "Reply",
        (),
        {
            "recipient_email": "user0@example.invalid",
            "payload_text": "只读回复",
        },
    )()

    sender.send(reply)

    assert smtp.message is not None
    assert smtp.message["To"] == "user0@example.invalid"
    assert smtp.message["Auto-Submitted"] == "auto-replied"
