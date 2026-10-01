from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from promptperp.accounting import (
    CashFlowGate,
    InvestorAccountingService,
    ReportFrequency,
)
from promptperp.storage import SQLiteAccountingStore


def main() -> None:
    now = datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory(prefix="promptperp-accounting-") as directory:
        store = SQLiteAccountingStore(Path(directory) / "accounting.sqlite3")
        service = InvestorAccountingService(store)
        service.initialize()
        service.register_investor(
            investor_id="user0",
            display_name="Offline Example User",
            email="user0@example.invalid",
            frequency=ReportFrequency.DAILY,
            timezone_name="Asia/Singapore",
            local_send_time="09:00",
            occurred_at=now,
        )
        service.create_pool(
            pool_id="example-pool",
            strategy_id="example-strategy",
            strategy_version="offline-v1",
            occurred_at=now,
        )
        service.contribute(
            event_id="example-contribution-001",
            investor_id="user0",
            pool_id="example-pool",
            amount=Decimal("1500"),
            gate=CashFlowGate(
                checked_at=now,
                reconciled_at=now,
                reconciliation_id="preflow-bootstrap",
            ),
            actor="offline-example",
            reason="demonstrate the investor ledger without external effects",
            external_reference="fake-transfer-001",
            occurred_at=now,
        )
        service.reconcile_and_value(
            reconciliation_id="example-reconciliation-001",
            occurred_at=now,
            exchange_equity=Decimal("1500"),
            unrealized_pnl={"example-pool": Decimal("0")},
        )
        view = service.investor_view("user0")
        print(
            json.dumps(
                {
                    "investor_id": view.investor_id,
                    "equity": str(view.equity),
                    "return_rate": str(view.return_rate),
                    "mode": "offline",
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
