from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Sequence

from promptperp.notifications import SQLiteInboxRegistry
from promptperp.reporting import EmailQueryHandler, InboundQuery
from promptperp.storage import SQLiteAccountingStore
from promptperp.storage.reporting_sqlite import SQLiteInvestorViewReader


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate one read-only email query; does not poll or send email"
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--inbox-state", required=True, type=Path)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--sender", required=True)
    parser.add_argument("--subject", default="")
    parser.add_argument("--body", default="")
    parser.add_argument("--received-at", required=True)
    parser.add_argument("--has-attachments", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    inbox = SQLiteInboxRegistry(args.inbox_state)
    inbox.initialize()
    reader = SQLiteInvestorViewReader(SQLiteAccountingStore(args.database))
    handler = EmailQueryHandler(reader, inbox)
    response = handler.handle(
        InboundQuery(
            message_id=args.message_id,
            sender=args.sender,
            subject=args.subject,
            body=args.body,
            received_at=datetime.fromisoformat(args.received_at),
            has_attachments=args.has_attachments,
        )
    )
    if response is not None:
        print(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
