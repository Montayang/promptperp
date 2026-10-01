from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from promptperp.domain import ReconciliationFailed
from promptperp.exchange import ExecutionGateway
from promptperp.execution.coordinator import ExecutionCoordinator
from promptperp.execution.lease import RunLease
from promptperp.execution.ledger import EventLedger
from promptperp.execution.models import ExecutionSnapshot, ExecutionState, TradeIntent
from promptperp.execution.ownership_sink import ExecutionOwnershipSink
from promptperp.execution.state_machine import ExecutionStateMachine


class ExecutionRegistry:
    """Create and recover one append-only execution ledger per trade intent."""

    def __init__(
        self,
        directory: str | Path,
        *,
        lease: RunLease,
        execution_gateway: ExecutionGateway,
        ownership_sink: ExecutionOwnershipSink | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.lease = lease
        self.execution_gateway = execution_gateway
        self.ownership_sink = ownership_sink

    def create(self, intent: TradeIntent) -> ExecutionCoordinator:
        self._require_run(intent.strategy_id, intent.run_id)
        unresolved = [
            snapshot.intent.intent_id
            for snapshot in self.snapshots()
            if snapshot.state is not ExecutionState.SETTLED
        ]
        if unresolved:
            raise ReconciliationFailed(
                "run has unresolved execution intents: " + ",".join(unresolved)
            )
        path = self._path(intent.intent_id)
        if path.exists():
            raise ReconciliationFailed("execution intent ledger already exists")
        return self._coordinator(path)

    def load(self, intent_id: str) -> ExecutionCoordinator:
        path = self._path(intent_id)
        if not path.exists():
            raise ReconciliationFailed("execution intent ledger is missing")
        coordinator = self._coordinator(path)
        snapshot = coordinator.state_machine.snapshot
        if snapshot is None or snapshot.intent.intent_id != intent_id:
            raise ReconciliationFailed(
                "execution intent ledger identity does not match"
            )
        self._require_run(snapshot.intent.strategy_id, snapshot.intent.run_id)
        return coordinator

    def snapshots(self) -> tuple[ExecutionSnapshot, ...]:
        self._require_run(self.lease.strategy_id, self.lease.run_id)
        if not self.directory.exists():
            return ()
        snapshots: list[ExecutionSnapshot] = []
        for path in sorted(self.directory.glob("*.jsonl")):
            snapshot = ExecutionStateMachine(EventLedger(path)).snapshot
            if snapshot is None:
                raise ReconciliationFailed("execution ledger is empty")
            self._require_run(snapshot.intent.strategy_id, snapshot.intent.run_id)
            snapshots.append(snapshot)
        return tuple(snapshots)

    def _coordinator(self, path: Path) -> ExecutionCoordinator:
        return ExecutionCoordinator(
            state_machine=ExecutionStateMachine(
                EventLedger(path),
                lease=self.lease,
            ),
            execution_gateway=self.execution_gateway,
            ownership_sink=self.ownership_sink,
        )

    def _path(self, intent_id: str) -> Path:
        if not intent_id:
            raise ValueError("intent ID is required")
        digest = sha256(intent_id.encode()).hexdigest()
        return self.directory / f"{digest}.jsonl"

    def _require_run(self, strategy_id: str, run_id: str) -> None:
        if not self.lease.acquired:
            raise ReconciliationFailed(
                "execution registry requires the active run lease"
            )
        if (strategy_id, run_id) != (self.lease.strategy_id, self.lease.run_id):
            raise ReconciliationFailed(
                "execution registry run ownership does not match"
            )
