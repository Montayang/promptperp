"""Bounded IMAP-to-SMTP worker for verified, read-only investor queries."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import smtplib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

from dotenv import dotenv_values

from promptperp.execution import RunLease
from promptperp.notifications import (
    IMAPInboxReader,
    IMAPSettings,
    SMTPQueryReplySender,
    SMTPSettings,
    SQLiteInboxRegistry,
)
from promptperp.operations.source_identity import (
    parse_timestamp,
    verify_source_identity,
)
from promptperp.reporting import EmailQueryHandler
from promptperp.storage import SQLiteAccountingStore
from promptperp.storage.reporting_sqlite import SQLiteInvestorViewReader


def _private_file(path: Path, label: str) -> None:
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(f"{label} must use owner-only permissions")


def _required(value: str | None, field: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _approval_window(approved_at: datetime, expires_at: datetime) -> None:
    now = datetime.now(timezone.utc)
    if approved_at > now or now >= expires_at:
        raise ValueError("email-query approval is not currently valid")
    if expires_at - approved_at > timedelta(hours=1):
        raise ValueError("email-query approval cannot exceed one hour")


def load_mail_only_environment(path: Path) -> Mapping[str, str | None]:
    values = dotenv_values(path)
    forbidden = {"API_KEY", "API_SECRET", "BASE_PATH"} & set(values)
    if forbidden:
        raise ValueError("mail worker environment must not contain trading settings")
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--inbox-state", required=True, type=Path)
    parser.add_argument("--mail-env-file", required=True, type=Path)
    parser.add_argument("--lease-path", type=Path)
    parser.add_argument("--commit-sha")
    parser.add_argument("--approved-at")
    parser.add_argument("--approval-expires-at")
    parser.add_argument("--max-messages", type=int, default=20)
    parser.add_argument("--execute", action="store_true")
    return parser


def _execute(args: argparse.Namespace, registry: SQLiteInboxRegistry) -> int:
    commit_sha = _required(args.commit_sha, "--commit-sha")
    approved_at = parse_timestamp(_required(args.approved_at, "--approved-at"))
    expires_at = parse_timestamp(
        _required(args.approval_expires_at, "--approval-expires-at")
    )
    _approval_window(approved_at, expires_at)
    verify_source_identity(commit_sha)
    _private_file(args.mail_env_file, "mail environment file")
    values = load_mail_only_environment(args.mail_env_file)
    account_email = _required(
        values.get("MAIL_ACCOUNT_EMAIL") or values.get("SENDER_EMAIL"),
        "MAIL_ACCOUNT_EMAIL",
    )
    password = _required(
        values.get("MAIL_APP_PASSWORD") or values.get("EMAIL_PASSWORD"),
        "MAIL_APP_PASSWORD",
    )
    imap = IMAPInboxReader(
        IMAPSettings(
            account_email=account_email,
            password=password,
            host=values.get("IMAP_HOST") or "imap.gmail.com",
            port=int(values.get("IMAP_PORT") or 993),
        )
    )
    smtp_settings = SMTPSettings(
        sender_email=account_email,
        password=password,
        host=values.get("SMTP_HOST") or "smtp.gmail.com",
        port=int(values.get("SMTP_PORT") or 587),
    )
    sender = SMTPQueryReplySender(smtp_settings, smtp_factory=smtplib.SMTP)
    view_reader = SQLiteInvestorViewReader(SQLiteAccountingStore(args.database))
    handler = EmailQueryHandler(view_reader, registry)

    queued = 0
    for query in imap.fetch_unseen(limit=args.max_messages):
        authorized = view_reader.investor_view_for_email(query.sender) is not None
        response = handler.handle(query)
        if authorized and response is not None:
            registry.enqueue_reply(
                message_id=query.message_id,
                recipient_email=query.sender,
                payload_text=response,
                occurred_at=datetime.now(timezone.utc),
            )
            queued += 1

    sent = 0
    for pending in registry.pending_replies()[: args.max_messages]:
        digest = hashlib.sha256(pending.reply_id.encode()).hexdigest()[:32]
        delivery_id = f"query-delivery:{digest}"
        claimed = registry.claim_reply(
            reply_id=pending.reply_id,
            delivery_id=delivery_id,
            occurred_at=datetime.now(timezone.utc),
        )
        sender.send(claimed)
        registry.mark_reply_sent(
            reply_id=claimed.reply_id,
            delivery_id=delivery_id,
            occurred_at=datetime.now(timezone.utc),
        )
        sent += 1
    print(json.dumps({"queued": queued, "sent": sent}, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.max_messages <= 100:
        raise ValueError("--max-messages must be between 1 and 100")
    registry = SQLiteInboxRegistry(args.inbox_state)
    registry.initialize()
    if not args.execute:
        print(
            json.dumps(
                {
                    "execute": False,
                    "pending_replies": len(registry.pending_replies()),
                },
                sort_keys=True,
            )
        )
        return 0
    lease = RunLease(
        args.lease_path or args.inbox_state.with_suffix(".provider.lease"),
        strategy_id="investor-email-query",
        run_id="imap-provider",
        owner_id=f"pid-{os.getpid()}",
    )
    with lease:
        return _execute(args, registry)


if __name__ == "__main__":
    raise SystemExit(main())
