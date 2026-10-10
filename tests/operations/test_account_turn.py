import json

import pytest

from promptperp.domain import ReconciliationFailed
from promptperp.operations.account_turn import SharedAccountBusy, SharedAccountTurn


def test_contention_never_enters_body_and_lock_can_be_reused(tmp_path):
    first = SharedAccountTurn(tmp_path, timeout_seconds=0.01)
    second = SharedAccountTurn(tmp_path, timeout_seconds=0.01)
    with first.acquire():
        with pytest.raises(SharedAccountBusy):
            with second.acquire():
                pytest.fail("contending worker entered account turn")
    with second.acquire():
        pass


def test_unknown_execution_is_not_transient_contention(tmp_path):
    turn = SharedAccountTurn(tmp_path)
    turn.barrier.write_text(json.dumps({"run_id": "owner"}))
    with pytest.raises(ReconciliationFailed) as error:
        with turn.acquire():
            pytest.fail("unknown execution was ignored")
    assert not isinstance(error.value, SharedAccountBusy)
    with turn.acquire(recovery=True):
        turn.verify_barrier("owner")
        with pytest.raises(ReconciliationFailed):
            turn.verify_barrier("foreign")
    turn.barrier.write_text("invalid")
    with pytest.raises(ReconciliationFailed):
        turn.verify_barrier("owner")
