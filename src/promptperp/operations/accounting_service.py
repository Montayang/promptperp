from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from promptperp.accounting import InvestorAccountingService, ReportFrequency
from promptperp.execution import RunLease
from promptperp.operations.accounting_recovery import (
    create_verified_backup,
    production_health,
)
from promptperp.reporting import InvestorReportingService, ReportScheduler
from promptperp.storage import SQLiteAccountingStore
from promptperp.storage.reporting_sqlite import SQLiteReportingStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline investor-accounting operations; never trades or sends email"
    )
    parser.add_argument("--database", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("health")
    backup = commands.add_parser("backup")
    backup.add_argument("--destination", required=True, type=Path)
    drill = commands.add_parser("restore-drill")
    drill.add_argument("--destination", required=True, type=Path)
    monitor = commands.add_parser("monitor")
    monitor.add_argument("--now")
    monitor.add_argument("--backup-manifest", type=Path)
    monitor.add_argument("--max-reconciliation-age-seconds", type=int, default=900)
    monitor.add_argument("--max-backup-age-seconds", type=int, default=172800)
    monitor.add_argument("--max-pending-outbox", type=int, default=100)
    show = commands.add_parser("show-investor")
    show.add_argument("--investor-id", required=True)
    statement = commands.add_parser("generate-statement")
    statement.add_argument("--investor-id", required=True)
    statement.add_argument(
        "--frequency", required=True, choices=[item.value for item in ReportFrequency]
    )
    statement.add_argument("--timezone", required=True)
    statement.add_argument("--due-at", required=True)
    scheduler = commands.add_parser("run-scheduler")
    scheduler.add_argument("--now")
    scheduler.add_argument("--lease-path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = SQLiteAccountingStore(args.database)
    accounting = InvestorAccountingService(store)
    if args.command == "init":
        accounting.initialize()
        print(json.dumps({"status": "initialized"}))
        return 0
    if args.command == "health":
        print(json.dumps(accounting.health(), sort_keys=True))
        return 0
    if args.command == "backup":
        manifest = create_verified_backup(
            store,
            destination=args.destination,
            occurred_at=datetime.now(timezone.utc),
        )
        print(
            json.dumps(
                {"status": "backed_up_and_verified", "manifest": str(manifest)},
                sort_keys=True,
            )
        )
        return 0
    if args.command == "restore-drill":
        manifest = create_verified_backup(
            store,
            destination=args.destination,
            occurred_at=datetime.now(timezone.utc),
        )
        print(
            json.dumps(
                {"status": "restore_drill_passed", "manifest": str(manifest)},
                sort_keys=True,
            )
        )
        return 0
    if args.command == "monitor":
        now = (
            datetime.now(timezone.utc)
            if args.now is None
            else datetime.fromisoformat(args.now)
        )
        health_result = production_health(
            store,
            now=now,
            maximum_reconciliation_age=timedelta(
                seconds=args.max_reconciliation_age_seconds
            ),
            maximum_pending_outbox=args.max_pending_outbox,
            backup_manifest=args.backup_manifest,
            maximum_backup_age=timedelta(seconds=args.max_backup_age_seconds),
        )
        print(
            json.dumps(
                {
                    "status": health_result.status,
                    "reasons": health_result.reasons,
                    "metrics": health_result.metrics,
                },
                sort_keys=True,
            )
        )
        return 0 if health_result.status == "HEALTHY" else 2
    if args.command == "show-investor":
        view = accounting.investor_view(args.investor_id)
        print(
            json.dumps(
                {
                    "investor_id": view.investor_id,
                    "strategy_id": view.strategy_id,
                    "equity": str(view.equity),
                    "return_rate": str(view.return_rate),
                    "snapshot_at": view.snapshot_at.isoformat(),
                    "reconciliation_id": view.reconciliation_id,
                },
                sort_keys=True,
            )
        )
        return 0
    reporting_store = SQLiteReportingStore(store)
    reporting = InvestorReportingService(accounting, reporting_store)
    if args.command == "run-scheduler":
        lease_path = args.lease_path or args.database.with_suffix(".scheduler.lease")
        lease = RunLease(
            lease_path,
            strategy_id="investor-accounting",
            run_id="report-scheduler",
            owner_id="one-shot-scheduler",
        )
        with lease:
            scheduler_now = (
                datetime.now(timezone.utc)
                if args.now is None
                else datetime.fromisoformat(args.now)
            )
            schedule_result = ReportScheduler(reporting, reporting_store).run_due(
                now=scheduler_now
            )
        print(
            json.dumps(
                {
                    "status": "queued_not_sent",
                    "initialized": schedule_result.initialized,
                    "generated": schedule_result.generated,
                    "message_ids": schedule_result.message_ids,
                },
                sort_keys=True,
            )
        )
        return 0
    message = reporting.generate(
        investor_id=args.investor_id,
        frequency=ReportFrequency(args.frequency),
        timezone_name=args.timezone,
        due_at=datetime.fromisoformat(args.due_at),
    )
    print(
        json.dumps(
            {
                "status": "queued_not_sent",
                "message_id": message.message_id,
                "statement_id": message.statement_id,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
