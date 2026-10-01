"""Validate or apply a one-time audited shared-pool opening migration."""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore[import-not-found]

from promptperp.accounting.bootstrap import (
    ImportedInvestorSnapshot,
    OpeningInvestor,
    SharedPoolBootstrap,
    bootstrap_shared_pool,
)
from promptperp.accounting.models import ReportFrequency
from promptperp.storage import SQLiteAccountingStore


def _table(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a TOML table")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _timestamp(value: object, name: str) -> datetime:
    parsed = datetime.fromisoformat(_text(value, name))
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


def load_bootstrap(path: str | Path) -> SharedPoolBootstrap:
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    migration = _table(raw.get("migration"), "migration")
    investor_rows = raw.get("investors")
    history_rows = raw.get("imported_snapshots", [])
    if not isinstance(investor_rows, list) or not isinstance(history_rows, list):
        raise ValueError("investors and imported_snapshots must be TOML arrays")
    investors = tuple(
        OpeningInvestor(
            investor_id=_text(row.get("investor_id"), "investor_id"),
            display_name=_text(row.get("display_name"), "display_name"),
            email=_text(row.get("email"), "email"),
            opening_contribution=Decimal(
                _text(row.get("opening_contribution"), "opening_contribution")
            ),
            opening_units=Decimal(_text(row.get("opening_units"), "opening_units")),
            frequency=ReportFrequency(
                _text(row.get("frequency", "monthly"), "frequency")
            ),
            timezone_name=_text(row.get("timezone", "Asia/Singapore"), "timezone"),
            local_send_time=_text(
                row.get("local_send_time", "15:00"), "local_send_time"
            ),
            monthly_send_day=int(row.get("monthly_send_day", 24)),
        )
        for value in investor_rows
        for row in (_table(value, "investor"),)
    )
    imported = tuple(
        ImportedInvestorSnapshot(
            record_id=_text(row.get("record_id"), "record_id"),
            investor_id=_text(row.get("investor_id"), "investor_id"),
            occurred_at=_timestamp(row.get("occurred_at"), "occurred_at"),
            units=Decimal(_text(row.get("units"), "units")),
            equity=Decimal(_text(row.get("equity"), "equity")),
            source_note=_text(row.get("source_note"), "source_note"),
        )
        for value in history_rows
        for row in (_table(value, "imported snapshot"),)
    )
    return SharedPoolBootstrap(
        migration_id=_text(migration.get("migration_id"), "migration_id"),
        pool_id=_text(migration.get("pool_id"), "pool_id"),
        strategy_id=_text(migration.get("strategy_id"), "strategy_id"),
        strategy_version=_text(migration.get("strategy_version"), "strategy_version"),
        occurred_at=_timestamp(migration.get("occurred_at"), "occurred_at"),
        exchange_equity=Decimal(
            _text(migration.get("exchange_equity"), "exchange_equity")
        ),
        investors=investors,
        imported_snapshots=imported,
        actor=_text(migration.get("actor", "operator"), "actor"),
        reason=_text(
            migration.get("reason", "audited opening balance migration"),
            "reason",
        ),
    )


def _require_private_file(path: Path) -> None:
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        raise PermissionError("bootstrap config must use owner-only permissions")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--database", default="log/investor_accounting.sqlite3")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config)
    _require_private_file(config_path)
    spec = load_bootstrap(config_path)
    if not args.apply:
        print("bootstrap_config_valid=true")
        print(f"investor_count={len(spec.investors)}")
        print(f"imported_snapshot_count={len(spec.imported_snapshots)}")
        print("database_changed=false")
        return 0
    store = SQLiteAccountingStore(args.database)
    store.initialize()
    result = bootstrap_shared_pool(store, spec)
    print("bootstrap_applied=true")
    print(f"pool_id={result.pool_id}")
    print(f"reconciliation_id={result.reconciliation_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
