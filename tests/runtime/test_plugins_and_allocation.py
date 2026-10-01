from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from promptperp.domain import OrderSide, PositionSide
from promptperp.runtime import (
    AllocationTarget,
    CapitalAllocationPlan,
    StrategyPluginRegistry,
    parameter_fingerprint,
)
from promptperp.strategies import PriceSnapshot

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_builtin_plugins_are_explicit_versioned_and_deterministic():
    registry = StrategyPluginRegistry()
    descriptors = registry.discover()

    assert [item.strategy_id for item in descriptors] == ["threshold_momentum"]
    assert all(item.interface_version == 1 for item in descriptors)
    assert all(len(item.fingerprint) == 64 for item in descriptors)
    with pytest.raises(ValueError, match="not registered"):
        registry.load("filesystem_plugin")


def test_sample_parameters_normalize_and_bind_fingerprint():
    plugin = StrategyPluginRegistry().load("threshold_momentum")

    normalized = plugin.normalize_parameters(
        {"schema_version": 1, "symbol": "btcusdt", "margin": "100"}
    )
    repeated = plugin.normalize_parameters(normalized)

    assert normalized["schema_version"] == 1
    assert normalized["symbol"] == "BTCUSDT"
    assert normalized["margin"] == "100"
    assert normalized == repeated
    assert parameter_fingerprint(normalized) == parameter_fingerprint(repeated)
    with pytest.raises(ValueError, match="migration"):
        plugin.normalize_parameters({"schema_version": 99})


def test_sample_plugin_emits_common_proposal():
    registry = StrategyPluginRegistry()
    momentum = registry.load("threshold_momentum").create(
        {"symbol": "BTCUSDT", "threshold_bps": "10", "margin": "100"}
    )

    assert (
        momentum.on_event(
            PriceSnapshot(observed_at=NOW, symbol="BTCUSDT", price=Decimal("100"))
        )
        is None
    )
    momentum_proposal = momentum.on_event(
        PriceSnapshot(
            observed_at=NOW + timedelta(seconds=1),
            symbol="BTCUSDT",
            price=Decimal("100.2"),
        )
    )
    assert momentum_proposal.strategy_id == "threshold_momentum"
    assert momentum_proposal.position_side is PositionSide.LONG


def test_momentum_supports_short_signal_and_rejects_wrong_event():
    instance = (
        StrategyPluginRegistry()
        .load("threshold_momentum")
        .create({"symbol": "BTCUSDT", "threshold_bps": "10"})
    )
    instance.on_event(
        PriceSnapshot(observed_at=NOW, symbol="BTCUSDT", price=Decimal("100"))
    )
    proposal = instance.on_event(
        PriceSnapshot(
            observed_at=NOW + timedelta(seconds=1),
            symbol="BTCUSDT",
            price=Decimal("99.8"),
        )
    )

    assert proposal.side is OrderSide.SELL
    assert proposal.position_side is PositionSide.SHORT
    with pytest.raises(TypeError, match="PriceSnapshot"):
        instance.on_event(object())


def test_allocation_plan_reserves_cash_and_rejects_overallocation():
    plan = CapitalAllocationPlan(
        allocatable_equity=Decimal("1000"),
        reserve_fraction=Decimal("0.1"),
        targets=(
            AllocationTarget(
                "external_plugin", Decimal("0.5"), Decimal("400"), 5, Decimal("60"), 1
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

    assert plan.budget("external_plugin") == Decimal("500.0")
    assert plan.budget("threshold_momentum") == Decimal("300.0")
    assert sum(plan.budgets.values()) == Decimal("800.0")
    with pytest.raises(ValueError, match="exceed"):
        CapitalAllocationPlan(
            allocatable_equity=Decimal("1000"),
            reserve_fraction=Decimal("0.2"),
            targets=(
                AllocationTarget(
                    "one", Decimal("0.5"), Decimal("100"), 2, Decimal("5"), 1
                ),
                AllocationTarget(
                    "two", Decimal("0.4"), Decimal("100"), 2, Decimal("5"), 1
                ),
            ),
        )
