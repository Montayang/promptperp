from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from promptperp.execution import LeaseUnavailable, RunLease
from promptperp.operations import (
    HealthState,
    LifecycleCommand,
    LifecycleController,
    LifecycleState,
    RunReport,
    RunSupervisor,
    StatusCorrupted,
    StatusStore,
    SupervisorError,
    redact,
    structured_log,
)

NOW = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


def make_supervisor(tmp_path, *, owner="worker-a", clock=None):
    lease = RunLease(
        tmp_path / "run.lease",
        strategy_id="sample_strategy",
        run_id="run-a6",
        owner_id=owner,
    )
    return RunSupervisor(
        status_path=tmp_path / "status.json",
        lease=lease,
        commit_sha="a" * 40,
        stale_after=timedelta(seconds=10),
        clock=clock,
    )


def test_lifecycle_rejects_duplicate_writer_and_status_is_side_effect_free(tmp_path):
    clock = MutableClock()
    first = make_supervisor(tmp_path, clock=clock)
    status = first.start()

    assert status.health is HealthState.NORMAL
    second = make_supervisor(tmp_path, owner="worker-b", clock=clock)
    with pytest.raises(LeaseUnavailable):
        second.start()

    before_status = (tmp_path / "status.json").read_bytes()
    before_lease = (tmp_path / "run.lease").read_bytes()
    inspection = RunSupervisor.inspect(
        status_path=tmp_path / "status.json",
        lease_path=tmp_path / "run.lease",
        now=NOW + timedelta(seconds=2),
        stale_after=timedelta(seconds=10),
    )
    assert inspection.writer_active
    assert inspection.detected_health is HealthState.NORMAL
    assert (tmp_path / "status.json").read_bytes() == before_status
    assert (tmp_path / "run.lease").read_bytes() == before_lease
    first.request_stop(reason="test complete")


def test_inspection_detects_crash_and_stale_heartbeat(tmp_path):
    clock = MutableClock()
    supervisor = make_supervisor(tmp_path, clock=clock)
    supervisor.start()
    supervisor.lease.release()

    inspection = RunSupervisor.inspect(
        status_path=tmp_path / "status.json",
        lease_path=tmp_path / "run.lease",
        now=NOW + timedelta(seconds=20),
        stale_after=timedelta(seconds=10),
    )

    assert inspection.detected_health is HealthState.BLOCKED
    assert inspection.reasons == ("HEARTBEAT_STALE", "WRITER_MISSING")


def test_health_states_are_distinguishable_and_invalid_combinations_fail(tmp_path):
    clock = MutableClock()
    supervisor = make_supervisor(tmp_path, clock=clock)
    supervisor.start()
    clock.value += timedelta(seconds=1)

    disabled = supervisor.heartbeat(health=HealthState.ENTRY_DISABLED)
    assert disabled.health is HealthState.ENTRY_DISABLED
    reconciling = supervisor.heartbeat(reconciliation_required=True)
    assert reconciling.health is HealthState.RECONCILIATION_REQUIRED
    blocked = supervisor.heartbeat(blocking_reasons=("ORDER_OUTCOME_UNKNOWN",))
    assert blocked.health is HealthState.BLOCKED
    still_blocked = supervisor.heartbeat()
    assert still_blocked.blocking_reasons == ("ORDER_OUTCOME_UNKNOWN",)
    recovered = supervisor.heartbeat(health=HealthState.NORMAL)
    assert recovered.health is HealthState.NORMAL

    with pytest.raises(ValueError, match="blocked status"):
        from dataclasses import replace

        replace(blocked, blocking_reasons=())
    with pytest.raises(ValueError, match="non-sensitive"):
        replace(recovered, metrics={"api_key": 1})
    supervisor.request_stop(reason="operator stop")


def test_safe_stop_preserves_ownership_and_protection(tmp_path):
    supervisor = make_supervisor(tmp_path, clock=MutableClock())
    supervisor.start()
    supervisor.heartbeat(
        owns_position=True,
        protection_confirmed=True,
        metrics={"fills": 1},
    )

    stopped = supervisor.request_stop(reason="maintenance")

    assert stopped.lifecycle is LifecycleState.STOP_REQUESTED
    assert stopped.health is HealthState.ENTRY_DISABLED
    assert stopped.owns_position
    assert stopped.protection_confirmed
    assert not supervisor.lease.acquired
    persisted = StatusStore(tmp_path / "status.json").read()
    assert persisted == stopped
    with pytest.raises(SupervisorError):
        supervisor.heartbeat()


def test_stop_does_not_hide_unresolved_reconciliation(tmp_path):
    supervisor = make_supervisor(tmp_path, clock=MutableClock())
    supervisor.start()
    supervisor.heartbeat(reconciliation_required=True)

    stopped = supervisor.request_stop(reason="operator stop")

    assert stopped.lifecycle is LifecycleState.STOP_REQUESTED
    assert stopped.health is HealthState.RECONCILIATION_REQUIRED
    assert stopped.reconciliation_required


def test_flat_stop_is_terminal_and_does_not_delete_status(tmp_path):
    supervisor = make_supervisor(tmp_path, clock=MutableClock())
    supervisor.start()
    stopped = supervisor.request_stop(reason="normal shutdown")

    assert stopped.lifecycle is LifecycleState.STOPPED
    assert stopped.health is HealthState.STOPPED
    assert (tmp_path / "status.json").exists()


def test_lifecycle_controller_exposes_narrow_commands(tmp_path):
    supervisor = make_supervisor(tmp_path, clock=MutableClock())
    controller = LifecycleController(supervisor)

    assert controller.execute(LifecycleCommand.START)["health"] == "NORMAL"
    status = controller.execute(LifecycleCommand.STATUS)
    assert status["writer_active"] is True
    assert status["detected_health"] == "NORMAL"
    stopped = controller.execute(LifecycleCommand.STOP, reason="done")
    assert stopped["health"] == "STOPPED"


def test_status_corruption_fails_closed(tmp_path):
    path = tmp_path / "status.json"
    path.write_text('{"schema_version": 1, "api_secret": "do-not-print"}')
    with pytest.raises(StatusCorrupted, match="cannot be trusted"):
        StatusStore(path).read()


def test_redaction_is_recursive_and_structured_logs_hide_secrets():
    raw = {
        "API_KEY": "key-value",
        "nested": {"password": "mail-value"},
        "message": "authorization=Bearer-credential strategy healthy",
        "safe": "BTCUSDT",
    }

    cleaned = redact(raw)
    encoded = structured_log("health", raw)

    assert cleaned["API_KEY"] == "[REDACTED]"
    assert cleaned["nested"]["password"] == "[REDACTED]"
    assert "key-value" not in encoded
    assert "mail-value" not in encoded
    assert "Bearer-credential" not in encoded
    assert json.loads(encoded)["safe"] == "BTCUSDT"


def test_machine_readable_report_has_only_attributed_aggregates():
    report = RunReport(
        schema_version=1,
        strategy_id="sample_strategy",
        run_id="run-a6",
        commit_sha="a" * 40,
        started_at=NOW,
        ended_at=NOW + timedelta(hours=1),
        final_health="STOPPED",
        intents=2,
        settled_trades=1,
        realized_pnl=Decimal("10"),
        commission=Decimal("1"),
        funding=Decimal("-0.5"),
        stop_reason="operator stop",
        unresolved_ownership=False,
    )

    payload = json.loads(report.to_json())
    assert payload["realized_pnl"] == "10"
    assert payload["commission"] == "1"
    assert "balance" not in payload
    assert "order_id" not in payload
