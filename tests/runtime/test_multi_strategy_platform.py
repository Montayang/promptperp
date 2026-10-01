from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

import pytest

from promptperp.domain import OrderSide, PositionSide
from promptperp.execution import LeaseUnavailable
from promptperp.runtime import (
    AccountOrder,
    AccountPosition,
    AccountSnapshot,
    AllocationTarget,
    CapitalAllocationPlan,
    ClaimState,
    ExecutionOutcome,
    MultiStrategyPlatform,
    OfflineStrategySession,
    PlatformMode,
    PlatformRunState,
    PlatformStateError,
    PortfolioGate,
    PortfolioPolicy,
    PositionClaim,
    SignalProposal,
    SQLitePlatformStore,
    StrategyDescriptor,
    StrategyPluginRegistry,
    ThresholdMomentumPlugin,
)
from promptperp.strategies import PriceSnapshot

NOW = datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc)


class _FixtureInstance:
    def __init__(self, parameters: Mapping[str, Any]):
        self._symbol = str(parameters["symbol"])
        self._margin = Decimal(str(parameters["margin"]))

    def on_event(self, event: object) -> SignalProposal | None:
        if not isinstance(event, PriceSnapshot):
            raise TypeError("fixture plugin requires a PriceSnapshot")
        return SignalProposal(
            strategy_id="external_fixture",
            symbol=self._symbol,
            side=OrderSide.BUY,
            position_side=PositionSide.LONG,
            margin=self._margin,
            leverage=5,
            take_profit_ratio=Decimal("0.01"),
            stop_loss_ratio=Decimal("0.005"),
            observed_at=event.observed_at,
            reason="test_fixture",
        )


class _FixturePlugin:
    descriptor = StrategyDescriptor(
        strategy_id="external_fixture",
        strategy_version="1.0.0",
        interface_version=1,
        parameter_schema_version=1,
        event_kind="price.v1",
        parameter_schema={
            "symbol": {"type": "string"},
            "margin": {"type": "decimal"},
        },
    )

    def migrate_parameters(self, raw: Mapping[str, Any]) -> Mapping[str, Any]:
        return dict(raw)

    def normalize_parameters(self, raw: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "schema_version": 1,
            "strategy_id": "external_fixture",
            "symbol": str(raw.get("symbol", "FUSDT")).upper(),
            "margin": str(raw.get("margin", "400")),
        }

    def create(self, parameters: Mapping[str, Any]) -> _FixtureInstance:
        return _FixtureInstance(self.normalize_parameters(parameters))


def make_platform(tmp_path) -> MultiStrategyPlatform:
    allocation = CapitalAllocationPlan(
        allocatable_equity=Decimal("1000"),
        reserve_fraction=Decimal("0.1"),
        targets=(
            AllocationTarget(
                "external_fixture", Decimal("0.4"), Decimal("400"), 5, Decimal("60"), 1
            ),
            AllocationTarget(
                "threshold_momentum",
                Decimal("0.3"),
                Decimal("100"),
                2,
                Decimal("5"),
                1,
            ),
        ),
    )
    policy = PortfolioPolicy(
        allowed_symbols=frozenset(
            {
                "AUSDT",
                "BUSDT",
                "CUSDT",
                "DUSDT",
                "EUSDT",
                "FUSDT",
                "BTCUSDT",
            }
        ),
        max_total_positions=2,
        max_total_margin=Decimal("500"),
        balance_buffer=Decimal("100"),
        max_signal_age_seconds=Decimal("30"),
        max_leverage=5,
    )
    platform = MultiStrategyPlatform(
        store=SQLitePlatformStore(tmp_path / "platform.sqlite3"),
        plugins=StrategyPluginRegistry((_FixturePlugin(), ThresholdMomentumPlugin())),
        allocation=allocation,
        portfolio_gate=PortfolioGate(policy),
    )
    platform.initialize()
    return platform


def flat_snapshot(*, balance: str = "900", observed_at: datetime = NOW):
    return AccountSnapshot(
        observed_at=observed_at,
        available_balance=Decimal(balance),
        reconciliation_ok=True,
    )


def plan_two(platform: MultiStrategyPlatform) -> None:
    platform.plan(
        strategy_id="external_fixture",
        run_id="fixture-run",
        raw_parameters={"symbol": "FUSDT", "margin": "400"},
        created_at=NOW,
    )
    platform.plan(
        strategy_id="threshold_momentum",
        run_id="momentum-run",
        raw_parameters={
            "symbol": "BTCUSDT",
            "threshold_bps": "10",
            "margin": "100",
        },
        created_at=NOW,
    )


def test_two_distinct_plugins_run_in_parallel_through_same_gates(tmp_path):
    platform = make_platform(tmp_path)
    plan_two(platform)
    fixture = OfflineStrategySession(
        platform=platform,
        run_id="fixture-run",
        lease_path=tmp_path / "fixture.lease",
        owner_id="worker-fixture",
    )
    momentum = OfflineStrategySession(
        platform=platform,
        run_id="momentum-run",
        lease_path=tmp_path / "momentum.lease",
        owner_id="worker-momentum",
    )
    fixture.start(occurred_at=NOW)
    momentum.start(occurred_at=NOW)

    fixture_proposal, fixture_decision = fixture.on_event(
        PriceSnapshot(
            observed_at=NOW + timedelta(seconds=7),
            symbol="FUSDT",
            price=Decimal("10"),
        ),
        quantity=Decimal("1"),
        snapshot=flat_snapshot(observed_at=NOW + timedelta(seconds=7)),
        evaluated_at=NOW + timedelta(seconds=7),
    )
    momentum.on_event(
        PriceSnapshot(observed_at=NOW, symbol="BTCUSDT", price=Decimal("100")),
        quantity=Decimal("2"),
        snapshot=flat_snapshot(),
        evaluated_at=NOW,
    )
    momentum_proposal, momentum_decision = momentum.on_event(
        PriceSnapshot(
            observed_at=NOW + timedelta(seconds=8),
            symbol="BTCUSDT",
            price=Decimal("100.2"),
        ),
        quantity=Decimal("2"),
        snapshot=AccountSnapshot(
            observed_at=NOW + timedelta(seconds=8),
            available_balance=Decimal("500"),
            reconciliation_ok=True,
            positions=(
                AccountPosition(
                    "FUSDT",
                    PositionSide.LONG,
                    Decimal("1"),
                    owner_strategy_id="external_fixture",
                    owner_run_id="fixture-run",
                ),
            ),
            open_orders=(
                AccountOrder(
                    "FUSDT",
                    reduce_only=True,
                    owner_strategy_id="external_fixture",
                    owner_run_id="fixture-run",
                ),
            ),
        ),
        evaluated_at=NOW + timedelta(seconds=8),
    )

    assert fixture_proposal.symbol == "FUSDT"
    assert fixture_decision.allowed
    assert momentum_proposal.symbol == "BTCUSDT"
    assert momentum_decision.allowed
    assert {claim.run_id for claim in platform.store.claims()} == {
        "fixture-run",
        "momentum-run",
    }
    assert platform.status("fixture-run").reserved_margin == Decimal("400")
    assert platform.status("momentum-run").reserved_margin == Decimal("100")
    fixture.close()
    momentum.close()


def test_duplicate_run_worker_and_active_strategy_are_rejected(tmp_path):
    platform = make_platform(tmp_path)
    platform.plan(
        strategy_id="external_fixture",
        run_id="fixture-run",
        raw_parameters={"symbol": "FUSDT", "margin": "400"},
        created_at=NOW,
    )
    first = OfflineStrategySession(
        platform=platform,
        run_id="fixture-run",
        lease_path=tmp_path / "run.lease",
        owner_id="worker-a",
    )
    second = OfflineStrategySession(
        platform=platform,
        run_id="fixture-run",
        lease_path=tmp_path / "run.lease",
        owner_id="worker-b",
    )
    first.lease.acquire()
    with pytest.raises(LeaseUnavailable):
        second.lease.acquire()
    first.close()
    with pytest.raises(PlatformStateError, match="active strategy"):
        platform.plan(
            strategy_id="external_fixture",
            run_id="fixture-run-two",
            raw_parameters={"symbol": "FUSDT", "margin": "200"},
            created_at=NOW,
        )


def test_transactional_portfolio_limit_survives_concurrent_claims(tmp_path):
    platform = make_platform(tmp_path)
    plan_two(platform)
    platform.start("fixture-run", occurred_at=NOW)
    platform.start("momentum-run", occurred_at=NOW)
    claims = (
        PositionClaim(
            "external_fixture",
            "fixture-run",
            "FUSDT",
            PositionSide.LONG,
            Decimal("100"),
            Decimal("1"),
            ClaimState.RESERVED,
            False,
            NOW,
        ),
        PositionClaim(
            "threshold_momentum",
            "momentum-run",
            "BTCUSDT",
            PositionSide.LONG,
            Decimal("100"),
            Decimal("1"),
            ClaimState.RESERVED,
            False,
            NOW,
        ),
    )

    def claim_position(claim):
        platform.store.add_claim(
            claim,
            maximum_total_positions=1,
            maximum_total_margin=Decimal("500"),
            occurred_at=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim_position, claim) for claim in claims]
    failures = [future.exception() for future in futures if future.exception()]

    assert len(platform.store.claims()) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], PlatformStateError)


def test_global_budget_symbol_conflict_and_foreign_objects_fail_closed(tmp_path):
    platform = make_platform(tmp_path)
    plan_two(platform)
    platform.start("fixture-run", occurred_at=NOW)
    platform.start("momentum-run", occurred_at=NOW)
    fixture_proposal = SignalProposal(
        strategy_id="external_fixture",
        symbol="FUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        margin=Decimal("400"),
        leverage=5,
        take_profit_ratio=Decimal("0.05"),
        stop_loss_ratio=Decimal("0.02"),
        observed_at=NOW,
        reason="test",
    )
    assert platform.authorize_open(
        run_id="fixture-run",
        proposal=fixture_proposal,
        quantity=Decimal("1"),
        snapshot=flat_snapshot(),
        evaluated_at=NOW,
    ).allowed

    same_symbol = SignalProposal(
        strategy_id="threshold_momentum",
        symbol="FUSDT",
        side=OrderSide.SELL,
        position_side=PositionSide.SHORT,
        margin=Decimal("100"),
        leverage=2,
        take_profit_ratio=Decimal("0.01"),
        stop_loss_ratio=Decimal("0.01"),
        observed_at=NOW,
        reason="conflict",
    )
    decision = platform.authorize_open(
        run_id="momentum-run",
        proposal=same_symbol,
        quantity=Decimal("1"),
        snapshot=flat_snapshot(),
        evaluated_at=NOW,
    )
    assert not decision.allowed
    assert "SYMBOL_ALREADY_OWNED" in decision.reason_codes

    foreign = AccountSnapshot(
        observed_at=NOW,
        available_balance=Decimal("900"),
        reconciliation_ok=True,
        positions=(AccountPosition("ETHUSDT", PositionSide.LONG, Decimal("1")),),
    )
    decision = platform.authorize_open(
        run_id="momentum-run",
        proposal=SignalProposal(
            strategy_id="threshold_momentum",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            position_side=PositionSide.LONG,
            margin=Decimal("100"),
            leverage=2,
            take_profit_ratio=Decimal("0.01"),
            stop_loss_ratio=Decimal("0.01"),
            observed_at=NOW,
            reason="foreign-test",
        ),
        quantity=Decimal("1"),
        snapshot=foreign,
        evaluated_at=NOW,
    )
    assert "FOREIGN_ACCOUNT_OBJECTS" in decision.reason_codes


def test_strategy_risk_budget_and_unresolved_reservation_cannot_be_bypassed(tmp_path):
    platform = make_platform(tmp_path)
    plan_two(platform)
    platform.start("fixture-run", occurred_at=NOW)
    platform.start("momentum-run", occurred_at=NOW)
    excessive = SignalProposal(
        strategy_id="threshold_momentum",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        margin=Decimal("100"),
        leverage=5,
        take_profit_ratio=Decimal("0.01"),
        stop_loss_ratio=Decimal("0.02"),
        observed_at=NOW,
        reason="excessive strategy risk",
    )
    rejected = platform.authorize_open(
        run_id="momentum-run",
        proposal=excessive,
        quantity=Decimal("1"),
        snapshot=flat_snapshot(),
        evaluated_at=NOW,
    )
    assert "STRATEGY_LEVERAGE_EXCEEDED" in rejected.reason_codes
    assert "STRATEGY_LOSS_BUDGET_EXCEEDED" in rejected.reason_codes

    fixture = SignalProposal(
        strategy_id="external_fixture",
        symbol="FUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        margin=Decimal("400"),
        leverage=5,
        take_profit_ratio=Decimal("0.010"),
        stop_loss_ratio=Decimal("0.005"),
        observed_at=NOW,
        reason="reserve before execution",
    )
    assert platform.authorize_open(
        run_id="fixture-run",
        proposal=fixture,
        quantity=Decimal("1"),
        snapshot=flat_snapshot(),
        evaluated_at=NOW,
    ).allowed
    blocked = platform.authorize_open(
        run_id="momentum-run",
        proposal=SignalProposal(
            strategy_id="threshold_momentum",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            position_side=PositionSide.LONG,
            margin=Decimal("100"),
            leverage=2,
            take_profit_ratio=Decimal("0.01"),
            stop_loss_ratio=Decimal("0.005"),
            observed_at=NOW,
            reason="must wait for recovery",
        ),
        quantity=Decimal("1"),
        snapshot=flat_snapshot(),
        evaluated_at=NOW,
    )
    assert "EXECUTION_RECOVERY_REQUIRED" in blocked.reason_codes

    with pytest.raises(PlatformStateError, match="still has a position"):
        platform.resolve_empty_reservation(
            run_id="fixture-run",
            symbol="FUSDT",
            snapshot=AccountSnapshot(
                observed_at=NOW,
                available_balance=Decimal("900"),
                reconciliation_ok=True,
                positions=(AccountPosition("FUSDT", PositionSide.LONG, Decimal("1")),),
            ),
            occurred_at=NOW,
        )
    platform.resolve_empty_reservation(
        run_id="fixture-run",
        symbol="FUSDT",
        snapshot=flat_snapshot(),
        occurred_at=NOW,
    )
    assert platform.store.claims() == ()


def test_kill_switch_blocks_entry_but_preserves_exact_owned_reduction(tmp_path):
    platform = make_platform(tmp_path)
    platform.plan(
        strategy_id="threshold_momentum",
        run_id="momentum-run",
        raw_parameters={"symbol": "BTCUSDT", "margin": "100"},
        created_at=NOW,
    )
    platform.start("momentum-run", occurred_at=NOW)
    proposal = SignalProposal(
        strategy_id="threshold_momentum",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        margin=Decimal("100"),
        leverage=2,
        take_profit_ratio=Decimal("0.01"),
        stop_loss_ratio=Decimal("0.01"),
        observed_at=NOW,
        reason="test",
    )
    platform.authorize_open(
        run_id="momentum-run",
        proposal=proposal,
        quantity=Decimal("2"),
        snapshot=flat_snapshot(),
        evaluated_at=NOW,
    )
    with pytest.raises(PlatformStateError, match="exactly match"):
        platform.confirm_position(
            run_id="momentum-run",
            symbol="BTCUSDT",
            quantity=Decimal("3"),
            protected=True,
            occurred_at=NOW,
        )
    platform.confirm_position(
        run_id="momentum-run",
        symbol="BTCUSDT",
        quantity=Decimal("2"),
        protected=True,
        occurred_at=NOW,
    )
    platform.set_mode(
        PlatformMode.KILL_SWITCH, reason="operator emergency", occurred_at=NOW
    )

    reduction = platform.authorize_reduction(
        run_id="momentum-run",
        symbol="BTCUSDT",
        quantity=Decimal("2"),
        evaluated_at=NOW,
    )
    excess = platform.authorize_reduction(
        run_id="momentum-run",
        symbol="BTCUSDT",
        quantity=Decimal("3"),
        evaluated_at=NOW,
    )
    assert reduction.allowed
    assert not excess.allowed
    with pytest.raises(PlatformStateError, match="NORMAL"):
        platform.start("unknown-run", occurred_at=NOW)


def test_reconciliation_blocks_all_runs_and_requires_explicit_mode_recovery(tmp_path):
    platform = make_platform(tmp_path)
    plan_two(platform)
    platform.start("fixture-run", occurred_at=NOW)
    platform.start("momentum-run", occurred_at=NOW)

    reasons = platform.reconcile(
        AccountSnapshot(
            observed_at=NOW,
            available_balance=Decimal("900"),
            reconciliation_ok=True,
            open_orders=(AccountOrder(symbol="XUSDT", reduce_only=False),),
        ),
        occurred_at=NOW,
    )

    assert "FOREIGN_ORDER_DETECTED" in reasons
    assert platform.store.mode() is PlatformMode.REDUCE_ONLY
    assert platform.status("fixture-run").state is PlatformRunState.BLOCKED
    assert platform.status("momentum-run").state is PlatformRunState.BLOCKED

    assert (
        platform.reconcile(flat_snapshot(), occurred_at=NOW + timedelta(seconds=1))
        == ()
    )
    assert platform.status("fixture-run").state is PlatformRunState.STOPPED
    assert platform.status("momentum-run").state is PlatformRunState.STOPPED
    assert platform.store.mode() is PlatformMode.REDUCE_ONLY


def test_blocked_stop_request_and_unprotected_position_are_not_hidden(tmp_path):
    platform = make_platform(tmp_path)
    platform.plan(
        strategy_id="threshold_momentum",
        run_id="momentum-run",
        raw_parameters={"symbol": "BTCUSDT", "margin": "100"},
        created_at=NOW,
    )
    platform.start("momentum-run", occurred_at=NOW)
    proposal = SignalProposal(
        strategy_id="threshold_momentum",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        margin=Decimal("100"),
        leverage=2,
        take_profit_ratio=Decimal("0.01"),
        stop_loss_ratio=Decimal("0.005"),
        observed_at=NOW,
        reason="protection recovery",
    )
    platform.authorize_open(
        run_id="momentum-run",
        proposal=proposal,
        quantity=Decimal("1"),
        snapshot=flat_snapshot(),
        evaluated_at=NOW,
    )
    platform.confirm_position(
        run_id="momentum-run",
        symbol="BTCUSDT",
        quantity=Decimal("1"),
        protected=False,
        occurred_at=NOW,
    )
    reasons = platform.reconcile(
        AccountSnapshot(
            observed_at=NOW,
            available_balance=Decimal("800"),
            reconciliation_ok=True,
            positions=(
                AccountPosition(
                    "BTCUSDT",
                    PositionSide.LONG,
                    Decimal("1"),
                    owner_strategy_id="threshold_momentum",
                    owner_run_id="momentum-run",
                ),
            ),
        ),
        occurred_at=NOW,
    )
    assert "PROTECTION_INCOMPLETE" in reasons
    stopped = platform.stop(
        "momentum-run", reason="operator stop while blocked", occurred_at=NOW
    )
    assert stopped.state is PlatformRunState.BLOCKED_STOP_REQUESTED
    assert "PROTECTION_INCOMPLETE" in stopped.blocking_reasons

    platform.mark_protected(run_id="momentum-run", symbol="BTCUSDT", occurred_at=NOW)
    recovered = platform.reconcile(
        AccountSnapshot(
            observed_at=NOW,
            available_balance=Decimal("800"),
            reconciliation_ok=True,
            positions=(
                AccountPosition(
                    "BTCUSDT",
                    PositionSide.LONG,
                    Decimal("1"),
                    owner_strategy_id="threshold_momentum",
                    owner_run_id="momentum-run",
                ),
            ),
            open_orders=(
                AccountOrder(
                    "BTCUSDT",
                    reduce_only=True,
                    owner_strategy_id="threshold_momentum",
                    owner_run_id="momentum-run",
                ),
            ),
        ),
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert recovered == ()
    assert platform.status("momentum-run").state is PlatformRunState.STOP_REQUESTED


def test_stop_settlement_and_report_preserve_owned_position_until_flat(tmp_path):
    platform = make_platform(tmp_path)
    platform.plan(
        strategy_id="threshold_momentum",
        run_id="momentum-run",
        raw_parameters={"symbol": "BTCUSDT", "margin": "100"},
        created_at=NOW,
    )
    platform.start("momentum-run", occurred_at=NOW)
    proposal = SignalProposal(
        strategy_id="threshold_momentum",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        margin=Decimal("100"),
        leverage=2,
        take_profit_ratio=Decimal("0.01"),
        stop_loss_ratio=Decimal("0.01"),
        observed_at=NOW,
        reason="test",
    )
    platform.authorize_open(
        run_id="momentum-run",
        proposal=proposal,
        quantity=Decimal("2"),
        snapshot=flat_snapshot(),
        evaluated_at=NOW,
    )
    platform.confirm_position(
        run_id="momentum-run",
        symbol="BTCUSDT",
        quantity=Decimal("2"),
        protected=True,
        occurred_at=NOW,
    )

    requested = platform.stop("momentum-run", reason="operator stop", occurred_at=NOW)
    assert requested.state is PlatformRunState.STOP_REQUESTED
    with pytest.raises(PlatformStateError, match="ownership"):
        platform.settle(
            ExecutionOutcome(
                run_id="momentum-run",
                symbol="BTCUSDT",
                quantity=Decimal("1"),
                expected_price=Decimal("100"),
                average_price=Decimal("100.1"),
                realized_pnl=Decimal("1"),
                commission=Decimal("0.1"),
                funding=Decimal("0"),
                closed_at=NOW + timedelta(seconds=30),
            )
        )
    platform.settle(
        ExecutionOutcome(
            run_id="momentum-run",
            symbol="BTCUSDT",
            quantity=Decimal("2"),
            expected_price=Decimal("100"),
            average_price=Decimal("100.1"),
            realized_pnl=Decimal("3"),
            commission=Decimal("0.2"),
            funding=Decimal("-0.1"),
            closed_at=NOW + timedelta(minutes=1),
            anomalies=("slow_fill",),
        )
    )
    stopped = platform.stop(
        "momentum-run",
        reason="flat after settlement",
        occurred_at=NOW + timedelta(minutes=1),
    )
    report = platform.report("momentum-run", generated_at=NOW + timedelta(minutes=2))

    assert stopped.state is PlatformRunState.STOPPED
    assert report.settled_trades == 1
    assert report.realized_pnl == Decimal("3")
    assert report.commission == Decimal("0.2")
    assert report.funding == Decimal("-0.1")
    assert report.maximum_slippage_bps == Decimal("10.000")
    assert report.anomalies == ("slow_fill",)


def test_allocation_rebalance_uses_new_plan_only_while_run_is_flat(tmp_path):
    platform = make_platform(tmp_path)
    platform.plan(
        strategy_id="threshold_momentum",
        run_id="momentum-run",
        raw_parameters={"symbol": "BTCUSDT", "margin": "80"},
        created_at=NOW,
    )
    replacement = MultiStrategyPlatform(
        store=platform.store,
        plugins=platform.plugins,
        allocation=CapitalAllocationPlan(
            allocatable_equity=Decimal("1200"),
            reserve_fraction=Decimal("0.1"),
            targets=(
                AllocationTarget(
                    "external_fixture",
                    Decimal("0.4"),
                    Decimal("400"),
                    5,
                    Decimal("60"),
                    1,
                ),
                AllocationTarget(
                    "threshold_momentum",
                    Decimal("0.4"),
                    Decimal("120"),
                    3,
                    Decimal("8"),
                    1,
                ),
            ),
        ),
        portfolio_gate=platform.portfolio_gate,
    )
    rebalanced = replacement.rebalance("momentum-run", occurred_at=NOW)
    assert rebalanced.capital_budget == Decimal("480.0")
    assert replacement.store.plan("momentum-run").max_margin_per_trade == Decimal("120")

    replacement.start("momentum-run", occurred_at=NOW)
    proposal = SignalProposal(
        strategy_id="threshold_momentum",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        margin=Decimal("80"),
        leverage=2,
        take_profit_ratio=Decimal("0.01"),
        stop_loss_ratio=Decimal("0.005"),
        observed_at=NOW,
        reason="rebalance gate",
    )
    replacement.authorize_open(
        run_id="momentum-run",
        proposal=proposal,
        quantity=Decimal("1"),
        snapshot=flat_snapshot(),
        evaluated_at=NOW,
    )
    with pytest.raises(PlatformStateError, match="only while flat"):
        replacement.rebalance("momentum-run", occurred_at=NOW)
