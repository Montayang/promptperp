from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from promptperp.runtime.allocation import CapitalAllocationPlan
from promptperp.runtime.models import (
    AccountSnapshot,
    ClaimState,
    ExecutionOutcome,
    PlatformMode,
    PlatformRunReport,
    PlatformRunState,
    PlatformRunStatus,
    PortfolioDecision,
    PositionClaim,
    RunPlan,
    SignalProposal,
)
from promptperp.runtime.plugins import StrategyPluginRegistry, parameter_fingerprint
from promptperp.runtime.portfolio import PortfolioGate
from promptperp.runtime.store import PlatformStateError, SQLitePlatformStore


class MultiStrategyPlatform:
    """Control plane for strategy lifecycle, allocation and account ownership."""

    def __init__(
        self,
        *,
        store: SQLitePlatformStore,
        plugins: StrategyPluginRegistry,
        allocation: CapitalAllocationPlan,
        portfolio_gate: PortfolioGate,
    ):
        self.store = store
        self.plugins = plugins
        self.allocation = allocation
        self.portfolio_gate = portfolio_gate

    def initialize(self) -> None:
        self.store.initialize()

    def validate(
        self, strategy_id: str, raw_parameters: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        plugin = self.plugins.load(strategy_id)
        normalized = plugin.normalize_parameters(raw_parameters)
        if str(normalized.get("strategy_id")) != strategy_id:
            raise ValueError("normalized strategy identity does not match plugin")
        return normalized

    def plan(
        self,
        *,
        strategy_id: str,
        run_id: str,
        raw_parameters: Mapping[str, Any],
        created_at: datetime,
    ) -> RunPlan:
        plugin = self.plugins.load(strategy_id)
        normalized = self.validate(strategy_id, raw_parameters)
        target = self.allocation.target(strategy_id)
        budget = self.allocation.budget(strategy_id)
        plan = RunPlan(
            strategy_id=strategy_id,
            run_id=run_id,
            plugin_fingerprint=plugin.descriptor.fingerprint,
            parameter_fingerprint=parameter_fingerprint(normalized),
            parameters=normalized,
            capital_budget=budget,
            max_margin_per_trade=target.max_margin_per_trade,
            max_leverage=target.max_leverage,
            max_loss_per_trade=target.max_loss_per_trade,
            max_positions=target.max_positions,
            created_at=created_at,
        )
        self.store.create_plan(plan)
        return plan

    def start(self, run_id: str, *, occurred_at: datetime) -> PlatformRunStatus:
        if self.store.mode() is not PlatformMode.NORMAL:
            raise PlatformStateError("new runs require NORMAL platform mode")
        return self.store.start(run_id, occurred_at=occurred_at)

    def rebalance(self, run_id: str, *, occurred_at: datetime) -> PlatformRunStatus:
        plan = self.store.plan(run_id)
        target = self.allocation.target(plan.strategy_id)
        self.store.update_run_allocation(
            run_id=run_id,
            capital_budget=self.allocation.budget(plan.strategy_id),
            max_margin_per_trade=target.max_margin_per_trade,
            max_leverage=target.max_leverage,
            max_loss_per_trade=target.max_loss_per_trade,
            max_positions=target.max_positions,
            occurred_at=occurred_at,
        )
        return self.store.status(run_id)

    def status(self, run_id: str) -> PlatformRunStatus:
        return self.store.status(run_id)

    def stop(
        self, run_id: str, *, reason: str, occurred_at: datetime
    ) -> PlatformRunStatus:
        return self.store.stop(run_id, reason=reason, occurred_at=occurred_at)

    def set_mode(
        self, mode: PlatformMode, *, reason: str, occurred_at: datetime
    ) -> None:
        self.store.set_mode(mode, reason=reason, occurred_at=occurred_at)

    def authorize_open(
        self,
        *,
        run_id: str,
        proposal: SignalProposal,
        quantity: Decimal,
        snapshot: AccountSnapshot,
        evaluated_at: datetime,
    ) -> PortfolioDecision:
        status = self.store.status(run_id)
        if status.state is not PlatformRunState.RUNNING:
            raise PlatformStateError("only a running strategy can request an opening")
        plan = self.store.plan(run_id)
        decision = self.portfolio_gate.evaluate_open(
            mode=self.store.mode(),
            plan=plan,
            proposal=proposal,
            snapshot=snapshot,
            claims=self.store.claims(),
            evaluated_at=evaluated_at,
        )
        if decision.allowed:
            self.store.add_claim(
                PositionClaim(
                    strategy_id=proposal.strategy_id,
                    run_id=run_id,
                    symbol=proposal.symbol,
                    position_side=proposal.position_side,
                    margin=proposal.margin,
                    quantity=quantity,
                    state=ClaimState.RESERVED,
                    protected=False,
                    claimed_at=evaluated_at,
                ),
                maximum_total_positions=self.portfolio_gate.policy.max_total_positions,
                maximum_total_margin=self.portfolio_gate.policy.max_total_margin,
                occurred_at=evaluated_at,
            )
        return decision

    def confirm_position(
        self,
        *,
        run_id: str,
        symbol: str,
        quantity: Decimal,
        protected: bool,
        occurred_at: datetime,
    ) -> None:
        self.store.confirm_position(
            run_id=run_id,
            symbol=symbol,
            quantity=quantity,
            protected=protected,
            occurred_at=occurred_at,
        )

    def resolve_empty_reservation(
        self,
        *,
        run_id: str,
        symbol: str,
        snapshot: AccountSnapshot,
        occurred_at: datetime,
    ) -> None:
        if not snapshot.reconciliation_ok:
            raise PlatformStateError("reservation recovery requires reconciliation")
        snapshot_age = Decimal(
            str((occurred_at - snapshot.observed_at).total_seconds())
        )
        if (
            snapshot_age < 0
            or snapshot_age > self.portfolio_gate.policy.max_account_age_seconds
        ):
            raise PlatformStateError("reservation recovery snapshot is stale")
        if any(
            position.symbol == symbol and position.quantity != 0
            for position in snapshot.positions
        ):
            raise PlatformStateError("reservation symbol still has a position")
        if any(order.symbol == symbol for order in snapshot.open_orders):
            raise PlatformStateError("reservation symbol still has an order")
        self.store.release_empty_reservation(
            run_id=run_id, symbol=symbol, occurred_at=occurred_at
        )

    def mark_protected(
        self, *, run_id: str, symbol: str, occurred_at: datetime
    ) -> None:
        self.store.mark_protected(run_id=run_id, symbol=symbol, occurred_at=occurred_at)

    def authorize_reduction(
        self,
        *,
        run_id: str,
        symbol: str,
        quantity: Decimal,
        evaluated_at: datetime,
    ) -> PortfolioDecision:
        return self.portfolio_gate.evaluate_reduction(
            run_id=run_id,
            symbol=symbol,
            quantity=quantity,
            claims=self.store.claims(),
            evaluated_at=evaluated_at,
        )

    def settle(self, outcome: ExecutionOutcome) -> None:
        self.store.settle_claim(outcome)

    def reconcile(
        self, snapshot: AccountSnapshot, *, occurred_at: datetime
    ) -> tuple[str, ...]:
        claims = self.store.claims()
        reasons: list[str] = []
        if not snapshot.reconciliation_ok:
            reasons.append("ACCOUNT_RECONCILIATION_FAILED")
        snapshot_age = Decimal(
            str((occurred_at - snapshot.observed_at).total_seconds())
        )
        if (
            snapshot_age < 0
            or snapshot_age > self.portfolio_gate.policy.max_account_age_seconds
        ):
            reasons.append("ACCOUNT_SNAPSHOT_STALE")
        reservations = tuple(
            claim for claim in claims if claim.state is ClaimState.RESERVED
        )
        if reservations:
            reasons.append("EXECUTION_RECOVERY_REQUIRED")
        if any(
            claim.state is ClaimState.OPEN and not claim.protected for claim in claims
        ):
            reasons.append("PROTECTION_INCOMPLETE")
        expected = {
            (claim.strategy_id, claim.run_id, claim.symbol, claim.position_side)
            for claim in claims
            if claim.state is ClaimState.OPEN
        }
        observed = {
            (
                position.owner_strategy_id,
                position.owner_run_id,
                position.symbol,
                position.position_side,
            )
            for position in snapshot.positions
            if position.quantity != 0
        }
        if any(owner is None or run is None for owner, run, _, _ in observed):
            reasons.append("FOREIGN_POSITION_DETECTED")
        if observed != expected:
            reasons.append("POSITION_OWNERSHIP_MISMATCH")
        claim_owners = {
            (claim.strategy_id, claim.run_id, claim.symbol) for claim in claims
        }
        if any(
            (order.owner_strategy_id, order.owner_run_id, order.symbol)
            not in claim_owners
            for order in snapshot.open_orders
        ):
            reasons.append("FOREIGN_ORDER_DETECTED")
        reduce_order_owners = {
            (order.owner_strategy_id, order.owner_run_id, order.symbol)
            for order in snapshot.open_orders
            if order.reduce_only
        }
        if any(
            claim.state is ClaimState.OPEN
            and claim.protected
            and (claim.strategy_id, claim.run_id, claim.symbol)
            not in reduce_order_owners
            for claim in claims
        ):
            reasons.append("PROTECTION_COVERAGE_MISSING")
        unique = tuple(dict.fromkeys(reasons))
        if unique:
            self.store.block_all(unique, occurred_at=occurred_at)
            if self.store.mode() is PlatformMode.NORMAL:
                self.store.set_mode(
                    PlatformMode.REDUCE_ONLY,
                    reason="automatic fail-closed reconciliation",
                    occurred_at=occurred_at,
                )
        else:
            self.store.reconciliation_passed(occurred_at=occurred_at)
        return unique

    def report(self, run_id: str, *, generated_at: datetime) -> PlatformRunReport:
        status = self.store.status(run_id)
        outcomes = self.store.outcomes(run_id)
        anomalies = tuple(
            anomaly for outcome in outcomes for anomaly in outcome.anomalies
        )
        return PlatformRunReport(
            strategy_id=status.strategy_id,
            run_id=run_id,
            generated_at=generated_at,
            settled_trades=len(outcomes),
            realized_pnl=sum(
                (outcome.realized_pnl for outcome in outcomes), Decimal("0")
            ),
            commission=sum((outcome.commission for outcome in outcomes), Decimal("0")),
            funding=sum((outcome.funding for outcome in outcomes), Decimal("0")),
            maximum_slippage_bps=max(
                (outcome.slippage_bps for outcome in outcomes), default=Decimal("0")
            ),
            anomalies=anomalies,
        )

    @staticmethod
    def public_status(status: PlatformRunStatus) -> Mapping[str, Any]:
        values = asdict(status)
        values["state"] = status.state.value
        values["capital_budget"] = str(status.capital_budget)
        values["reserved_margin"] = str(status.reserved_margin)
        values["started_at"] = (
            None if status.started_at is None else status.started_at.isoformat()
        )
        values["updated_at"] = status.updated_at.isoformat()
        return values
