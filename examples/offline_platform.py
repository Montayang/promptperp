"""Run the bundled example strategy through the control plane without networking."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from promptperp.runtime import (
    AccountSnapshot,
    AllocationTarget,
    CapitalAllocationPlan,
    MultiStrategyPlatform,
    OfflineStrategySession,
    PortfolioGate,
    PortfolioPolicy,
    SQLitePlatformStore,
    StrategyPluginRegistry,
)
from promptperp.strategies import PriceSnapshot


def main() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        platform = MultiStrategyPlatform(
            store=SQLitePlatformStore(root / "platform.sqlite3"),
            plugins=StrategyPluginRegistry(),
            allocation=CapitalAllocationPlan(
                allocatable_equity=Decimal("1000"),
                reserve_fraction=Decimal("0.2"),
                targets=(
                    AllocationTarget(
                        "threshold_momentum",
                        Decimal("0.8"),
                        Decimal("100"),
                        2,
                        Decimal("5"),
                        1,
                    ),
                ),
            ),
            portfolio_gate=PortfolioGate(
                PortfolioPolicy(
                    allowed_symbols=frozenset({"BTCUSDT"}),
                    max_total_positions=1,
                    max_total_margin=Decimal("100"),
                    balance_buffer=Decimal("100"),
                    max_signal_age_seconds=Decimal("30"),
                    max_leverage=2,
                )
            ),
        )
        platform.initialize()
        platform.plan(
            strategy_id="threshold_momentum",
            run_id="offline-sample",
            raw_parameters={
                "symbol": "BTCUSDT",
                "threshold_bps": "10",
                "margin": "100",
            },
            created_at=now,
        )
        session = OfflineStrategySession(
            platform=platform,
            run_id="offline-sample",
            lease_path=root / "sample.lease",
            owner_id="offline-sample-worker",
        )
        session.start(occurred_at=now)
        snapshot = AccountSnapshot(now, Decimal("1000"), True)
        session.on_event(
            PriceSnapshot(now, "BTCUSDT", Decimal("100")),
            quantity=Decimal("1"),
            snapshot=snapshot,
            evaluated_at=now,
        )
        proposal, decision = session.on_event(
            PriceSnapshot(now + timedelta(seconds=1), "BTCUSDT", Decimal("100.2")),
            quantity=Decimal("1"),
            snapshot=AccountSnapshot(now + timedelta(seconds=1), Decimal("1000"), True),
            evaluated_at=now + timedelta(seconds=1),
        )
        print(
            json.dumps(
                {
                    "allowed": decision.allowed,
                    "execution_permitted": False,
                    "mode": "offline",
                    "strategy_id": proposal.strategy_id,
                    "symbol": proposal.symbol,
                },
                sort_keys=True,
            )
        )
        session.close()


if __name__ == "__main__":
    main()
