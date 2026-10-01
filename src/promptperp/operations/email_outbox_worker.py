"""Explicitly approved SMTP delivery for ledger outbox messages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from dotenv import dotenv_values

from promptperp.execution import RunLease
from promptperp.notifications import SMTPOutboxSender, SMTPSettings
from promptperp.operations.source_identity import (
    parse_timestamp,
    verify_source_identity,
)
from promptperp.reporting.delivery import OutboxDeliveryService
from promptperp.storage import SQLiteAccountingStore
from promptperp.storage.reporting_sqlite import SQLiteReportingStore


def _private_file(path: Path, label: str) -> None:
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(f"{label} must use owner-only permissions")


def _approval_window(approved_at: datetime, expires_at: datetime) -> None:
    now = datetime.now(timezone.utc)
    if approved_at > now or now >= expires_at:
        raise ValueError("email delivery approval is not currently valid")
    if expires_at - approved_at > timedelta(hours=1):
        raise ValueError("email delivery approval cannot exceed one hour")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--env-file", default=".env", type=Path)
    parser.add_argument("--lease-path", type=Path)
    parser.add_argument("--message-id")
    parser.add_argument("--delivery-id")
    parser.add_argument(
        "--next",
        action="store_true",
        help="deliver only the oldest safely pending message",
    )
    parser.add_argument("--commit-sha")
    parser.add_argument("--approved-at")
    parser.add_argument("--approval-expires-at")
    parser.add_argument("--test-label", action="store_true")
    parser.add_argument("--execute", action="store_true")
    return parser


def _required(value: str | None, flag: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"{flag} is required with --execute")
    return value.strip()


def load_mail_only_environment(path: Path) -> dict[str, str | None]:
    values = dict(dotenv_values(path))
    forbidden = {"API_KEY", "API_SECRET", "BASE_PATH"} & set(values)
    if forbidden:
        raise ValueError("mail worker environment must not contain trading settings")
    return values


def _select_message_id(
    reporting_store: SQLiteReportingStore,
    *,
    message_id: str | None,
    select_next: bool,
) -> str | None:
    if message_id and select_next:
        raise ValueError("use either --message-id or --next, not both")
    if message_id:
        return message_id.strip()
    if not select_next:
        raise ValueError("--message-id or --next is required with --execute")
    pending = reporting_store.pending_messages()
    return pending[0].message_id if pending else None


def _delivery_id(message_id: str, approved_at: datetime) -> str:
    source = f"{message_id}\n{approved_at.isoformat()}".encode()
    return f"scheduled-email:{hashlib.sha256(source).hexdigest()[:32]}"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    accounting_store = SQLiteAccountingStore(args.database)
    accounting_store.initialize()
    accounting_store.integrity_check()
    accounting_store.audit_ledger()
    reporting_store = SQLiteReportingStore(accounting_store)
    if not args.execute:
        print(
            json.dumps(
                {
                    "execute": False,
                    "pending_count": len(reporting_store.pending_messages()),
                },
                sort_keys=True,
            )
        )
        return 0

    commit_sha = _required(args.commit_sha, "--commit-sha")
    approved_at = parse_timestamp(_required(args.approved_at, "--approved-at"))
    expires_at = parse_timestamp(
        _required(args.approval_expires_at, "--approval-expires-at")
    )
    _approval_window(approved_at, expires_at)
    verify_source_identity(commit_sha)
    message_id = _select_message_id(
        reporting_store,
        message_id=args.message_id,
        select_next=args.next,
    )
    if message_id is None:
        print(json.dumps({"execute": True, "status": "NO_PENDING"}, sort_keys=True))
        return 0
    delivery_id = args.delivery_id or _delivery_id(message_id, approved_at)
    _private_file(args.env_file, "environment file")
    values = load_mail_only_environment(args.env_file)
    settings = SMTPSettings(
        sender_email=_required(values.get("SENDER_EMAIL"), "SENDER_EMAIL"),
        password=_required(values.get("EMAIL_PASSWORD"), "EMAIL_PASSWORD"),
    )
    prefix = "[PromptPerp 测试]" if args.test_label else "[PromptPerp]"
    lease = RunLease(
        args.lease_path or args.database.with_suffix(".email-outbox.lease"),
        strategy_id="investor-email-outbox",
        run_id="smtp-provider",
        owner_id=f"pid-{os.getpid()}",
    )
    with lease:
        service = OutboxDeliveryService(
            reporting_store,
            SMTPOutboxSender(settings, subject_prefix=prefix),
        )
        result = service.deliver(
            message_id=message_id,
            delivery_id=delivery_id,
            occurred_at=datetime.now(timezone.utc),
        )
    print(
        json.dumps(
            {
                "delivery_id": result.delivery_id,
                "message_id": result.message_id,
                "status": result.status,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
