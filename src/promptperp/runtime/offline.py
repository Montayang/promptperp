from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from promptperp.execution import RunLease
from promptperp.runtime.models import AccountSnapshot, PortfolioDecision, SignalProposal
from promptperp.runtime.platform import MultiStrategyPlatform
from promptperp.runtime.plugins import StrategyInstance


class OfflineStrategySession:
    """Deterministic no-network harness using the same platform gates."""

    def __init__(
        self,
        *,
        platform: MultiStrategyPlatform,
        run_id: str,
        lease_path: str | Path,
        owner_id: str,
    ):
        self.platform = platform
        self.run_id = run_id
        plan = platform.store.plan(run_id)
        plugin = platform.plugins.load(plan.strategy_id)
        self.instance: StrategyInstance = plugin.create(plan.parameters)
        self.lease = RunLease(
            lease_path,
            strategy_id=plan.strategy_id,
            run_id=run_id,
            owner_id=owner_id,
        )

    def start(self, *, occurred_at: datetime) -> None:
        self.lease.acquire()
        try:
            self.platform.start(self.run_id, occurred_at=occurred_at)
        except Exception:
            self.lease.release()
            raise

    def on_event(
        self,
        event: object,
        *,
        quantity: Decimal,
        snapshot: AccountSnapshot,
        evaluated_at: datetime,
    ) -> tuple[SignalProposal | None, PortfolioDecision | None]:
        if not self.lease.acquired:
            raise RuntimeError("offline strategy session is not running")
        proposal = self.instance.on_event(event)
        if proposal is None:
            return None, None
        decision = self.platform.authorize_open(
            run_id=self.run_id,
            proposal=proposal,
            quantity=quantity,
            snapshot=snapshot,
            evaluated_at=evaluated_at,
        )
        if decision.allowed:
            self.platform.confirm_position(
                run_id=self.run_id,
                symbol=proposal.symbol,
                quantity=quantity,
                protected=True,
                occurred_at=evaluated_at,
            )
        return proposal, decision

    def close(self) -> None:
        self.lease.release()
