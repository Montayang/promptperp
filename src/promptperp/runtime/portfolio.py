from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from promptperp.runtime.models import (
    AccountSnapshot,
    ClaimState,
    PlatformMode,
    PortfolioDecision,
    PositionClaim,
    RunPlan,
    SignalProposal,
)


@dataclass(frozen=True)
class PortfolioPolicy:
    allowed_symbols: frozenset[str]
    max_total_positions: int
    max_total_margin: Decimal
    balance_buffer: Decimal
    max_signal_age_seconds: Decimal
    max_leverage: int
    max_account_age_seconds: Decimal = Decimal("15")
    shared_symbol_policy: str = "EXCLUSIVE"

    def __post_init__(self) -> None:
        if not self.allowed_symbols:
            raise ValueError("portfolio symbol allowlist cannot be empty")
        if self.max_total_positions <= 0 or self.max_leverage <= 0:
            raise ValueError("portfolio count and leverage limits must be positive")
        decimals = (
            self.max_total_margin,
            self.balance_buffer,
            self.max_signal_age_seconds,
            self.max_account_age_seconds,
        )
        if not all(value.is_finite() and value >= 0 for value in decimals):
            raise ValueError("portfolio decimal limits must be finite and non-negative")
        if (
            self.max_total_margin <= 0
            or self.max_signal_age_seconds <= 0
            or self.max_account_age_seconds <= 0
        ):
            raise ValueError("portfolio margin and signal age limits must be positive")
        if self.shared_symbol_policy != "EXCLUSIVE":
            raise ValueError("v1 supports only exclusive symbol ownership")
        object.__setattr__(
            self, "allowed_symbols", frozenset(x.upper() for x in self.allowed_symbols)
        )


class PortfolioGate:
    """Global account gate that every strategy proposal must pass."""

    def __init__(self, policy: PortfolioPolicy):
        self.policy = policy

    def evaluate_open(
        self,
        *,
        mode: PlatformMode,
        plan: RunPlan,
        proposal: SignalProposal,
        snapshot: AccountSnapshot,
        claims: tuple[PositionClaim, ...],
        evaluated_at: datetime,
    ) -> PortfolioDecision:
        reasons: list[str] = []
        if mode is PlatformMode.KILL_SWITCH:
            reasons.append("KILL_SWITCH_ACTIVE")
        elif mode is PlatformMode.REDUCE_ONLY:
            reasons.append("REDUCE_ONLY_MODE")
        if not snapshot.reconciliation_ok:
            reasons.append("ACCOUNT_RECONCILIATION_FAILED")
        account_age = Decimal(
            str((evaluated_at - snapshot.observed_at).total_seconds())
        )
        if account_age < 0 or account_age > self.policy.max_account_age_seconds:
            reasons.append("ACCOUNT_SNAPSHOT_STALE")
        if any(claim.state is ClaimState.RESERVED for claim in claims):
            reasons.append("EXECUTION_RECOVERY_REQUIRED")
        if any(
            claim.state is ClaimState.OPEN and not claim.protected for claim in claims
        ):
            reasons.append("PROTECTION_INCOMPLETE")
        if self._has_foreign_objects(snapshot, claims):
            reasons.append("FOREIGN_ACCOUNT_OBJECTS")
        expected_positions = {
            (claim.strategy_id, claim.run_id, claim.symbol, claim.position_side)
            for claim in claims
            if claim.state is ClaimState.OPEN
        }
        observed_positions = {
            (
                position.owner_strategy_id,
                position.owner_run_id,
                position.symbol,
                position.position_side,
            )
            for position in snapshot.positions
            if position.quantity != 0
        }
        if observed_positions != expected_positions:
            reasons.append("OWNED_POSITION_MISMATCH")
        order_owners = {
            (order.owner_strategy_id, order.owner_run_id, order.symbol)
            for order in snapshot.open_orders
            if order.reduce_only
        }
        if any(
            claim.state is ClaimState.OPEN
            and claim.protected
            and (claim.strategy_id, claim.run_id, claim.symbol) not in order_owners
            for claim in claims
        ):
            reasons.append("PROTECTION_COVERAGE_MISSING")
        if proposal.strategy_id != plan.strategy_id:
            reasons.append("STRATEGY_ID_MISMATCH")
        if proposal.symbol not in self.policy.allowed_symbols:
            reasons.append("SYMBOL_NOT_ALLOWED")
        if proposal.margin > plan.max_margin_per_trade:
            reasons.append("PER_TRADE_MARGIN_EXCEEDED")
        if proposal.leverage > plan.max_leverage:
            reasons.append("STRATEGY_LEVERAGE_EXCEEDED")
        planned_loss = (
            proposal.margin * Decimal(proposal.leverage) * proposal.stop_loss_ratio
        )
        if planned_loss > plan.max_loss_per_trade:
            reasons.append("STRATEGY_LOSS_BUDGET_EXCEEDED")
        reserved_for_run = sum(
            (claim.margin for claim in claims if claim.run_id == plan.run_id),
            Decimal("0"),
        )
        if reserved_for_run + proposal.margin > plan.capital_budget:
            reasons.append("RUN_CAPITAL_BUDGET_EXCEEDED")
        run_positions = sum(1 for claim in claims if claim.run_id == plan.run_id)
        if run_positions >= plan.max_positions:
            reasons.append("STRATEGY_POSITION_LIMIT_REACHED")
        total_margin = sum((claim.margin for claim in claims), Decimal("0"))
        if total_margin + proposal.margin > self.policy.max_total_margin:
            reasons.append("PORTFOLIO_MARGIN_EXCEEDED")
        if len(claims) >= self.policy.max_total_positions:
            reasons.append("PORTFOLIO_POSITION_LIMIT_REACHED")
        if any(claim.symbol == proposal.symbol for claim in claims):
            reasons.append("SYMBOL_ALREADY_OWNED")
        if proposal.leverage > self.policy.max_leverage:
            reasons.append("PORTFOLIO_LEVERAGE_EXCEEDED")
        if snapshot.available_balance - self.policy.balance_buffer < proposal.margin:
            reasons.append("ACCOUNT_BALANCE_BUFFER_BREACHED")
        age = Decimal(str((evaluated_at - proposal.observed_at).total_seconds()))
        if age < 0 or age > self.policy.max_signal_age_seconds:
            reasons.append("SIGNAL_STALE")
        return PortfolioDecision(
            allowed=not reasons,
            strategy_id=plan.strategy_id,
            run_id=plan.run_id,
            symbol=proposal.symbol,
            reason_codes=tuple(reasons or ["ALLOWED"]),
            decided_at=evaluated_at,
        )

    @staticmethod
    def evaluate_reduction(
        *,
        run_id: str,
        symbol: str,
        quantity: Decimal,
        claims: tuple[PositionClaim, ...],
        evaluated_at: datetime,
    ) -> PortfolioDecision:
        owned = next(
            (
                claim
                for claim in claims
                if claim.run_id == run_id
                and claim.symbol == symbol
                and claim.state is ClaimState.OPEN
            ),
            None,
        )
        reasons: list[str] = []
        if owned is None:
            reasons.append("REDUCTION_OWNERSHIP_UNPROVEN")
        elif quantity <= 0 or quantity > owned.quantity:
            reasons.append("REDUCTION_EXCEEDS_OWNED_POSITION")
        return PortfolioDecision(
            allowed=not reasons,
            strategy_id="unknown" if owned is None else owned.strategy_id,
            run_id=run_id,
            symbol=symbol,
            reason_codes=tuple(reasons or ["ALLOWED"]),
            decided_at=evaluated_at,
        )

    @staticmethod
    def _has_foreign_objects(
        snapshot: AccountSnapshot, claims: tuple[PositionClaim, ...]
    ) -> bool:
        owners = {(claim.strategy_id, claim.run_id, claim.symbol) for claim in claims}
        for position in snapshot.positions:
            identity = (
                position.owner_strategy_id,
                position.owner_run_id,
                position.symbol,
            )
            if position.quantity != 0 and identity not in owners:
                return True
        for order in snapshot.open_orders:
            identity = (order.owner_strategy_id, order.owner_run_id, order.symbol)
            if identity not in owners:
                return True
        return False
