from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Protocol

from promptperp.reporting.models import OutboxMessage


class EmailSender(Protocol):
    def send(self, message: OutboxMessage) -> None:
        """Send one pre-rendered message; production use requires operator approval."""


class SMTPConnection(Protocol):
    def ehlo(self) -> object: ...

    def starttls(self) -> object: ...

    def login(self, user: str, password: str) -> object: ...

    def send_message(self, message: EmailMessage) -> object: ...

    def quit(self) -> object: ...


class SMTPFactory(Protocol):
    def __call__(self, host: str, port: int, *, timeout: float) -> SMTPConnection: ...


def _mailbox(value: str, field: str) -> str:
    normalized = value.strip()
    _, parsed = parseaddr(normalized)
    if (
        not normalized
        or parsed != normalized
        or "@" not in parsed
        or "\r" in normalized
        or "\n" in normalized
    ):
        raise ValueError(f"{field} is not a plain email address")
    return normalized


@dataclass(frozen=True)
class SMTPSettings:
    sender_email: str
    password: str
    host: str = "smtp.gmail.com"
    port: int = 587
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        _mailbox(self.sender_email, "sender_email")
        if not self.password:
            raise ValueError("SMTP password is required")
        if not self.host or not 1 <= self.port <= 65535:
            raise ValueError("SMTP endpoint is invalid")
        if self.timeout_seconds <= 0:
            raise ValueError("SMTP timeout must be positive")


class SMTPOutboxSender:
    """TLS SMTP adapter; recipient always comes from a claimed ledger message."""

    def __init__(
        self,
        settings: SMTPSettings,
        *,
        subject_prefix: str = "[PromptPerp]",
        smtp_factory: SMTPFactory = smtplib.SMTP,
    ):
        if "\r" in subject_prefix or "\n" in subject_prefix:
            raise ValueError("subject prefix cannot contain newlines")
        self._settings = settings
        self._subject_prefix = subject_prefix.strip()
        self._smtp_factory = smtp_factory

    def send(self, message: OutboxMessage) -> None:
        recipient = _mailbox(message.recipient_email, "recipient_email")
        sender = _mailbox(self._settings.sender_email, "sender_email")
        email = EmailMessage()
        email["Subject"] = f"{self._subject_prefix} 资金报告"
        email["From"] = sender
        email["To"] = recipient
        email.set_content(message.payload_text)

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
            server.send_message(email)
        finally:
            server.quit()
