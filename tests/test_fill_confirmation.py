from decimal import Decimal

import pytest

from promptperp.domain import FuturesPosition, PositionSide, ReconciliationFailed
from promptperp.execution.confirmation import FillConfirmation

KEY = ("BTCUSDT", PositionSide.LONG)


def positions(amount):
    return (
        (FuturesPosition(*KEY, Decimal(amount), Decimal(100)),)
        if Decimal(amount)
        else ()
    )


def test_lagging_history_keeps_same_refresh_scope_until_exact_confirmation():
    owned = {KEY: Decimal(1)}
    scopes, audit = [], []

    def refresh(scope):
        scopes.append(scope)
        # First query still lags; second must query the same symbol again.
        if len(scopes) == 2:
            owned[KEY] = Decimal(2)

    verifier = FillConfirmation(
        read_positions=lambda: positions(2),
        read_owned=lambda: owned,
        refresh=refresh,
        audit=audit.append,
        sleep=lambda _: None,
    )
    verifier.confirm(before={KEY: Decimal(1)}, expected={KEY: Decimal(2)})
    assert scopes == [frozenset({"BTCUSDT"})] * 2
    assert len(audit) == 3
    assert audit[-1].differences == ()


@pytest.mark.parametrize("old,want", [(0, 2), (2, 1), (2, 0)])
def test_increase_reduce_and_full_exit_converge(old, want):
    owned, actual = {KEY: Decimal(old)}, [positions(old)]

    def refresh(scope):
        assert scope == frozenset({"BTCUSDT"})
        owned[KEY], actual[0] = Decimal(want), positions(want)

    FillConfirmation(
        read_positions=lambda: actual[0],
        read_owned=lambda: owned,
        refresh=refresh,
        audit=lambda _: None,
        sleep=lambda _: None,
    ).confirm(before={KEY: Decimal(old)}, expected={KEY: Decimal(want)})


@pytest.mark.parametrize(
    "actual",
    [
        positions(3),
        positions("0.99999999"),
        (FuturesPosition("ETHUSDT", PositionSide.LONG, Decimal(1), Decimal(1)),),
        (FuturesPosition("BTCUSDT", PositionSide.SHORT, Decimal(1), Decimal(1)),),
        positions(1) + positions(1),
    ],
)
def test_unexplained_positions_fail_without_waiting(actual):
    waits, audits = [], []
    verifier = FillConfirmation(
        read_positions=lambda: actual,
        read_owned=lambda: {KEY: Decimal(2)},
        refresh=lambda _: pytest.fail("must not retry unrelated mismatch"),
        audit=audits.append,
        sleep=waits.append,
    )
    with pytest.raises(ReconciliationFailed):
        verifier.confirm(before={KEY: Decimal(1)}, expected={KEY: Decimal(2)})
    assert waits == []


def test_persistent_lag_is_bounded_and_not_silently_accepted():
    attempts = []
    verifier = FillConfirmation(
        read_positions=lambda: positions(1),
        read_owned=lambda: {KEY: Decimal(1)},
        refresh=lambda _: None,
        audit=attempts.append,
        sleep=lambda _: None,
    )
    with pytest.raises(ReconciliationFailed):
        verifier.confirm(before={KEY: Decimal(1)}, expected={KEY: Decimal(2)})
    assert len(attempts) == 3


def test_deadline_prevents_extra_reads_after_slow_callback():
    now = [0.0]

    def read():
        now[0] = 6
        return positions(1)

    verifier = FillConfirmation(
        read_positions=read,
        read_owned=lambda: {KEY: Decimal(1)},
        refresh=lambda _: pytest.fail("deadline must stop refresh"),
        audit=lambda _: None,
        sleep=lambda _: pytest.fail("deadline must stop sleep"),
        monotonic=lambda: now[0],
    )
    with pytest.raises(ReconciliationFailed, match="deadline"):
        verifier.confirm(before={KEY: Decimal(1)}, expected={KEY: Decimal(2)})


def test_audit_failure_blocks_success():
    def audit(_):
        raise OSError("synthetic storage failure")

    with pytest.raises(OSError):
        FillConfirmation(
            read_positions=lambda: positions(2),
            read_owned=lambda: {KEY: Decimal(2)},
            refresh=lambda _: None,
            audit=audit,
        ).confirm(before={KEY: Decimal(1)}, expected={KEY: Decimal(2)})
