from __future__ import annotations

import smtplib
import socket

import pytest

SENSITIVE_ENVIRONMENT_VARIABLES = (
    "API_KEY",
    "API_SECRET",
    "BASE_PATH",
    "EMAIL_PASSWORD",
    "PROMPTPERP_ENABLE_LIVE",
    "RECEIVER_EMAIL",
    "SENDER_EMAIL",
)


@pytest.fixture(autouse=True)
def offline_test_environment(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Make every test offline, credential-free, and runtime-state isolated."""

    for name in SENSITIVE_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    def blocked_external_effect(*_args, **_kwargs):
        raise AssertionError("offline tests must not access the network or SMTP")

    monkeypatch.setattr(socket, "create_connection", blocked_external_effect)
    monkeypatch.setattr(socket.socket, "connect", blocked_external_effect)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_external_effect)
    monkeypatch.setattr(smtplib, "SMTP", blocked_external_effect)

    yield

    assert not (tmp_path / "log").exists()
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "outputs").exists()
