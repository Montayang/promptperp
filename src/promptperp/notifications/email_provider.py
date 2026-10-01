from __future__ import annotations

import imaplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from typing import Callable, Protocol, cast

from promptperp.notifications.email_inbox import QueryReply
from promptperp.notifications.email_outbox import SMTPFactory, SMTPSettings
from promptperp.reporting.queries import InboundQuery


class IMAPConnection(Protocol):
    def login(self, user: str, password: str) -> object: ...

    def select(self, mailbox: str, readonly: bool = False) -> tuple[object, object]: ...

    def uid(self, command: str, *args: object) -> tuple[object, object]: ...

    def logout(self) -> object: ...


IMAPFactory = Callable[..., IMAPConnection]


def _plain_mailbox(value: str, field: str) -> str:
    normalized = value.strip()
    _, parsed = parseaddr(normalized)
    if (
        not normalized
        or parsed.casefold() != normalized.casefold()
        or "@" not in parsed
        or "\r" in normalized
        or "\n" in normalized
    ):
        raise ValueError(f"{field} is not a plain email address")
    return parsed.casefold()


@dataclass(frozen=True)
class IMAPSettings:
    account_email: str
    password: str
    host: str = "imap.gmail.com"
    port: int = 993
    mailbox: str = "INBOX"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        _plain_mailbox(self.account_email, "account_email")
        if not self.password:
            raise ValueError("IMAP password is required")
        if not self.host or not 1 <= self.port <= 65535:
            raise ValueError("IMAP endpoint is invalid")
        if not self.mailbox or self.timeout_seconds <= 0:
            raise ValueError("IMAP mailbox or timeout is invalid")


class IMAPInboxReader:
    """Read unseen messages without changing provider mailbox state."""

    def __init__(
        self,
        settings: IMAPSettings,
        *,
        imap_factory: IMAPFactory = cast(IMAPFactory, imaplib.IMAP4_SSL),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._settings = settings
        self._imap_factory = imap_factory
        self._clock = clock

    def fetch_unseen(self, *, limit: int = 20) -> tuple[InboundQuery, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("IMAP fetch limit must be between 1 and 100")
        connection = self._imap_factory(
            self._settings.host,
            self._settings.port,
            timeout=self._settings.timeout_seconds,
        )
        try:
            connection.login(
                self._settings.account_email,
                self._settings.password,
            )
            status, _ = connection.select(self._settings.mailbox, readonly=True)
            if str(status).upper() != "OK":
                raise RuntimeError("IMAP mailbox cannot be opened read-only")
            status, values = connection.uid("search", None, "UNSEEN")
            if str(status).upper() != "OK":
                raise RuntimeError("IMAP unseen search failed")
            identifiers = self._search_ids(values)[-limit:]
            messages: list[InboundQuery] = []
            for identifier in identifiers:
                status, payload = connection.uid("fetch", identifier, "(BODY.PEEK[])")
                if str(status).upper() != "OK":
                    raise RuntimeError("IMAP message fetch failed")
                raw = self._message_bytes(payload)
                parsed = self._parse(raw)
                if parsed is not None:
                    messages.append(parsed)
            return tuple(messages)
        finally:
            connection.logout()

    @staticmethod
    def _search_ids(values: object) -> list[bytes]:
        if not isinstance(values, list) or not values:
            return []
        first = values[0]
        if not isinstance(first, bytes):
            raise RuntimeError("IMAP search response is malformed")
        return first.split()

    @staticmethod
    def _message_bytes(payload: object) -> bytes:
        if not isinstance(payload, list):
            raise RuntimeError("IMAP fetch response is malformed")
        for item in payload:
            if (
                isinstance(item, tuple)
                and len(item) >= 2
                and isinstance(item[1], bytes)
            ):
                return item[1]
        raise RuntimeError("IMAP fetch response has no message body")

    def _parse(self, raw: bytes) -> InboundQuery | None:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if message.get("Auto-Submitted", "no").casefold() != "no":
            return None
        if (
            message.get("List-Id")
            or message.get("Resent-From")
            or message.get("Sender")
        ):
            return None
        from_values = message.get_all("From", [])
        if len(from_values) != 1:
            return None
        try:
            sender = _plain_mailbox(str(from_values[0]), "sender")
        except ValueError:
            return None
        reply_to = message.get("Reply-To")
        if reply_to is not None:
            try:
                if _plain_mailbox(str(reply_to), "reply_to") != sender:
                    return None
            except ValueError:
                return None
        message_id = str(message.get("Message-ID", "")).strip()
        if not message_id or len(message_id) > 998:
            return None
        received_at = self._clock()
        raw_date = message.get("Date")
        if raw_date:
            try:
                candidate = parsedate_to_datetime(str(raw_date))
                if candidate.tzinfo is not None:
                    received_at = candidate.astimezone(timezone.utc)
            except (TypeError, ValueError):
                pass
        subject = str(message.get("Subject", ""))
        attachments = False
        bodies: list[str] = []
        if message.is_multipart():
            for part in message.walk():
                if part.is_multipart():
                    continue
                if part.get_content_disposition() == "attachment":
                    attachments = True
                    continue
                if part.get_content_type() != "text/plain":
                    attachments = True
                    continue
                value = part.get_content()
                if isinstance(value, str):
                    bodies.append(value)
        elif message.get_content_type() == "text/plain":
            value = message.get_content()
            if isinstance(value, str):
                bodies.append(value)
        else:
            attachments = True
        body = "\n".join(bodies).strip()
        if len(subject) > 256 or len(body) > 4096:
            attachments = True
            subject = ""
            body = ""
        return InboundQuery(
            message_id=message_id,
            sender=sender,
            subject=subject,
            body=body,
            received_at=received_at,
            has_attachments=attachments,
        )


class SMTPQueryReplySender:
    def __init__(
        self,
        settings: SMTPSettings,
        *,
        smtp_factory: SMTPFactory,
    ) -> None:
        self._settings = settings
        self._smtp_factory = smtp_factory

    def send(self, reply: QueryReply) -> None:
        recipient = _plain_mailbox(reply.recipient_email, "recipient_email")
        sender = _plain_mailbox(self._settings.sender_email, "sender_email")
        message = EmailMessage()
        message["Subject"] = "[PromptPerp] 只读查询回复"
        message["From"] = sender
        message["To"] = recipient
        message["Auto-Submitted"] = "auto-replied"
        message.set_content(reply.payload_text)
        server = self._smtp_factory(
            self._settings.host,
            self._settings.port,
            timeout=self._settings.timeout_seconds,
        )
        try:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender, self._settings.password)
            server.send_message(message)
        finally:
            server.quit()
