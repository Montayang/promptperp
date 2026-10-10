import pytest

from promptperp.domain import ReconciliationFailed, RequestUnknown
from promptperp.operations.settlement_gate import SettlementGate, ValuationPending


def pending():
    raise ValuationPending("synthetic settlement batch pending")


def test_pending_does_not_authorize_trading_and_restart_keeps_deadline(tmp_path):
    now = [100.0]
    path = tmp_path / "gate.db"
    gate = SettlementGate(path, max_wait_seconds=10, clock=lambda: now[0])
    with pytest.raises(ValuationPending):
        gate.reconcile(pending)
    now[0] = 109
    restarted = SettlementGate(path, max_wait_seconds=10, clock=lambda: now[0])
    with pytest.raises(ValuationPending):
        restarted.reconcile(pending)
    now[0] = 110
    with pytest.raises(ReconciliationFailed, match="expired"):
        restarted.reconcile(lambda: pytest.fail("deadline already expired"))
    with pytest.raises(ReconciliationFailed, match="faulted"):
        gate.reconcile(lambda: None)


def test_full_verified_reconciliation_clears_only_temporary_hold(tmp_path):
    now = [100.0]
    gate = SettlementGate(
        tmp_path / "gate.db", max_wait_seconds=10, clock=lambda: now[0]
    )
    with pytest.raises(ValuationPending):
        gate.reconcile(pending)
    now[0] = 105
    gate.reconcile(lambda: None)
    now[0] = 120
    gate.reconcile(lambda: None)


@pytest.mark.parametrize(
    "error",
    [
        RequestUnknown("unknown order"),
        ReconciliationFailed("foreign position"),
        OSError("storage failed"),
    ],
)
def test_other_faults_are_not_reclassified_as_transient(tmp_path, error):
    path = tmp_path / "gate.db"
    gate = SettlementGate(path, max_wait_seconds=10, clock=lambda: 100)

    def fail():
        raise error

    with pytest.raises(type(error)):
        gate.reconcile(fail)
    with pytest.raises(ReconciliationFailed, match="faulted"):
        SettlementGate(path, max_wait_seconds=10, clock=lambda: 101).reconcile(
            lambda: None
        )


def test_slow_callback_cannot_extend_pending_deadline(tmp_path):
    now = [100.0]
    gate = SettlementGate(
        tmp_path / "gate.db", max_wait_seconds=10, clock=lambda: now[0]
    )

    def slow():
        now[0] += 11
        pending()

    with pytest.raises(ReconciliationFailed, match="expired"):
        gate.reconcile(slow)


def test_policy_change_and_clock_rollback_do_not_reset_hold(tmp_path):
    now = [100.0]
    path = tmp_path / "gate.db"
    gate = SettlementGate(path, max_wait_seconds=10, clock=lambda: now[0])
    gate.reconcile(lambda: None)
    now[0] = 99
    with pytest.raises(ReconciliationFailed):
        gate.reconcile(lambda: pytest.fail("clock rollback"))
    with pytest.raises(ReconciliationFailed):
        SettlementGate(path, max_wait_seconds=20).reconcile(lambda: None)
