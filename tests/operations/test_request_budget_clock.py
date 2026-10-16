"""Deterministic SQLite interleavings, without network or live worker wiring."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

import promptperp.operations.request_budget as module
from promptperp.operations.request_budget import BudgetUnavailable, RequestBudget


def test_reservation_samples_time_after_a_competing_writer_commits(tmp_path):
    now = [100.0]
    path = tmp_path / "budget.db"
    budget = RequestBudget(path, capacity=10, window_seconds=10, clock=lambda: now[0])
    connect = sqlite3.connect
    published = False

    def publish_then_connect(*args, **kwargs):
        nonlocal published
        if not published:
            published = True
            now[0] = 101.0
            with connect(path) as writer:
                writer.execute("INSERT INTO reservations VALUES (121, 4)")
                writer.execute("UPDATE policy SET last_time=101")
        return connect(*args, **kwargs)

    with patch.object(module.sqlite3, "connect", side_effect=publish_then_connect):
        permit = budget.reserve(6)
    assert permit.issued_at == 101.0
    with pytest.raises(BudgetUnavailable, match="exhausted"):
        budget.reserve(1)


def test_reservation_clock_sample_follows_actual_sqlite_lock_wait(tmp_path):
    now = [100.0]
    path = tmp_path / "budget.db"
    budget = RequestBudget(path, capacity=10, window_seconds=10, clock=lambda: now[0])
    connect = sqlite3.connect
    connected = threading.Event()
    sampled = threading.Event()

    def clock():
        sampled.set()
        return now[0]

    budget.clock = clock

    def notify_connection(*args, **kwargs):
        db = connect(*args, **kwargs)
        connected.set()
        return db

    with connect(path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with patch.object(module.sqlite3, "connect", side_effect=notify_connection):
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(budget.reserve, 6)
                try:
                    assert connected.wait(2)
                    assert not sampled.is_set()
                    now[0] = 101.0
                    writer.execute("INSERT INTO reservations VALUES (121, 4)")
                    writer.execute("UPDATE policy SET last_time=101")
                finally:
                    writer.commit()
                permit = result.result(timeout=3)
    assert sampled.is_set() and permit.issued_at == 101.0
    # True clock rollback still blocks, without adding a tolerance or leaking a lock.
    now[0] = 100.0
    with pytest.raises(BudgetUnavailable, match="backwards"):
        budget.reserve(1)
    now[0] = 121.0
    assert budget.reserve(10).remaining == 10
