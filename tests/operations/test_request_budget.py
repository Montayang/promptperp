from concurrent.futures import ThreadPoolExecutor

import pytest

from promptperp.operations.account_turn import SharedAccountTurn
from promptperp.operations.request_budget import BudgetUnavailable, RequestBudget


def test_whole_turn_includes_final_settlement_and_second_snapshot(tmp_path):
    now = [100.0]
    budget = RequestBudget(
        tmp_path / "budget.db", capacity=100, window_seconds=60, clock=lambda: now[0]
    )
    # Synthetic costs: preflight + order + bounded reads + settlement + snapshot.
    costs = [20, 1, 7, 40, 20]
    permit = budget.reserve(sum(costs))
    with SharedAccountTurn(tmp_path / "turn").acquire():
        for cost in costs:
            permit.consume(cost)
    assert permit.remaining == 0
    with pytest.raises(BudgetUnavailable):
        permit.consume(1)
    # Another worker cannot borrow already-admitted capacity.
    other = RequestBudget(
        tmp_path / "budget.db", capacity=100, window_seconds=60, clock=lambda: now[0]
    )
    with pytest.raises(BudgetUnavailable):
        other.reserve(13)
    # A late request must remain accounted for after the permit expires.
    now[0] = 160
    with pytest.raises(BudgetUnavailable):
        other.reserve(13)
    now[0] = 220
    assert other.reserve(100).remaining == 100


def test_no_lock_or_network_work_when_admission_fails(tmp_path):
    budget = RequestBudget(tmp_path / "budget.db", capacity=10, window_seconds=60)
    turn = SharedAccountTurn(tmp_path / "turn")
    budget.reserve(10)
    with pytest.raises(BudgetUnavailable):
        budget.reserve(1)
        with turn.acquire():
            pytest.fail("must defer before entering account turn")
    assert not turn.directory.exists()


def test_shared_workers_atomically_reserve_capacity(tmp_path):
    path = tmp_path / "budget.db"
    budget = RequestBudget(path, capacity=10, window_seconds=60, clock=lambda: 100)

    def reserve(_):
        try:
            budget.reserve(6)
            return True
        except BudgetUnavailable:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(reserve, range(2))) == [False, True]


def test_policy_restart_expiry_and_clock_rollback_fail_closed(tmp_path):
    now = [100.0]
    path = tmp_path / "budget.db"
    budget = RequestBudget(path, capacity=10, window_seconds=60, clock=lambda: now[0])
    permit = budget.reserve(5)
    with pytest.raises(BudgetUnavailable, match="policy"):
        RequestBudget(path, capacity=20, window_seconds=60)
    now[0] = 99
    with pytest.raises(BudgetUnavailable):
        budget.reserve(1)
    with pytest.raises(BudgetUnavailable):
        permit.consume(1)


def test_clock_rollback_within_permit_window_is_rejected(tmp_path):
    now = [100.0]
    budget = RequestBudget(
        tmp_path / "budget.db", capacity=10, window_seconds=60, clock=lambda: now[0]
    )
    permit = budget.reserve(5)
    now[0] = 110
    permit.consume(1)
    now[0] = 109
    with pytest.raises(BudgetUnavailable):
        permit.consume(1)
    now[0] = 160
    with pytest.raises(BudgetUnavailable):
        permit.consume(1)


@pytest.mark.parametrize("weight", [0, -1, 11, True, 1.5])
def test_invalid_weights_never_issue_permit(tmp_path, weight):
    budget = RequestBudget(tmp_path / "budget.db", capacity=10, window_seconds=60)
    with pytest.raises(BudgetUnavailable):
        budget.reserve(weight)
