from __future__ import annotations

import importlib
import pkgutil
import smtplib
import socket
from pathlib import Path

import pytest


def test_all_modules_import_without_loading_dotenv(monkeypatch, tmp_path):
    import dotenv

    calls = []

    def forbidden_dotenv_load(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("project imports must not load a .env file")

    monkeypatch.setattr(dotenv, "load_dotenv", forbidden_dotenv_load)
    package_root = Path(__file__).resolve().parents[1] / "src" / "promptperp"
    modules = tuple(
        item.name
        for item in pkgutil.walk_packages([str(package_root)], prefix="promptperp.")
    )

    for module_name in modules:
        importlib.import_module(module_name)

    assert modules
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_offline_harness_blocks_network_and_smtp():
    with pytest.raises(AssertionError, match="offline tests"):
        socket.create_connection(("127.0.0.1", 1))
    with pytest.raises(AssertionError, match="offline tests"):
        smtplib.SMTP("127.0.0.1", 25)
