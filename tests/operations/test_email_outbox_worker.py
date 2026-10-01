from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from promptperp.operations.email_outbox_worker import (
    _delivery_id,
    _select_message_id,
    load_mail_only_environment,
)


class PendingStore:
    def __init__(self, *message_ids: str):
        self._messages = tuple(
            SimpleNamespace(message_id=message_id) for message_id in message_ids
        )

    def pending_messages(self):
        return self._messages


def test_next_selects_only_oldest_pending_message():
    store = PendingStore("email:first", "email:second")

    assert _select_message_id(store, message_id=None, select_next=True) == "email:first"
    assert _select_message_id(PendingStore(), message_id=None, select_next=True) is None


def test_explicit_and_next_selection_are_mutually_exclusive():
    with pytest.raises(ValueError, match="either"):
        _select_message_id(PendingStore(), message_id="email:first", select_next=True)
    with pytest.raises(ValueError, match="required"):
        _select_message_id(PendingStore(), message_id=None, select_next=False)


def test_scheduled_delivery_identity_is_deterministic():
    approved_at = datetime(2026, 10, 1, tzinfo=timezone.utc)

    first = _delivery_id("email:first", approved_at)
    second = _delivery_id("email:first", approved_at)

    assert first == second
    assert first.startswith("scheduled-email:")


def test_mail_environment_rejects_trading_credentials(tmp_path):
    path = tmp_path / "mail.env"
    path.write_text("SENDER_EMAIL=x@example.invalid\nAPI_KEY=not-a-real-key\n")

    with pytest.raises(ValueError, match="trading settings"):
        load_mail_only_environment(path)
