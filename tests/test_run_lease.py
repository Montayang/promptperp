from __future__ import annotations

import json

import pytest

from promptperp.execution import LeaseUnavailable, RunLease


def lease(path, owner):
    return RunLease(
        path,
        strategy_id="sample",
        run_id="run-001",
        owner_id=owner,
    )


def test_second_writer_cannot_acquire_active_run(tmp_path):
    path = tmp_path / "run.lock"
    first = lease(path, "worker-a")
    second = lease(path, "worker-b")
    first.acquire()

    with pytest.raises(LeaseUnavailable, match="already has a writer"):
        second.acquire()

    metadata = json.loads(path.read_text(encoding="utf-8"))
    assert metadata["owner_id"] == "worker-a"
    first.release()


def test_run_can_be_reacquired_after_clean_release(tmp_path):
    path = tmp_path / "run.lock"

    with lease(path, "worker-a"):
        pass

    second = lease(path, "worker-b")
    second.acquire()
    assert second.acquired is True
    second.release()
