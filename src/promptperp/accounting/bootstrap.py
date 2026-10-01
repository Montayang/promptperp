from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from promptperp.accounting import chart
from promptperp.accounting.errors import AccountingValidationError
from promptperp.accounting.models import (
    MONEY_QUANTUM,
    UNIT_QUANTUM,
    EntryDirection,
    LedgerTransaction,
    Posting,
    ReconciliationResult,
    ReconciliationStatus,
    ReportFrequency,
    SourceEvent,
    require_identifier,
    require_money,
    require_utc,
)
from promptperp.accounting.valuation import value_pool
from promptperp.storage.accounting_sqlite import SQLiteAccountingStore


def _require_units(value: Decimal, field: str) -> None:
    if not value.is_finite() or value <= 0:
        raise AccountingValidationError(f"{field} must be finite and positive")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -18:
        raise AccountingValidationError(f"{field} exceeds unit precision")


@dataclass(frozen=True)
class OpeningInvestor:
    investor_id: str
    display_name: str
    email: str
    opening_contribution: Decimal
    opening_units: Decimal
    frequency: ReportFrequency = ReportFrequency.MONTHLY
    timezone_name: str = "Asia/Singapore"
    local_send_time: str = "15:00"
    monthly_send_day: int = 24

    def __post_init__(self) -> None:
        require_identifier(self.investor_id, "investor_id")
        require_identifier(self.display_name, "display_name")
        if "@" not in self.email.strip():
            raise AccountingValidationError("investor email is invalid")
        require_money(self.opening_contribution, "opening contribution", positive=True)
        _require_units(self.opening_units, "opening units")
        if not 1 <= self.monthly_send_day <= 28:
            raise AccountingValidationError("monthly send day must be between 1 and 28")
        try:
            parsed_time = time.fromisoformat(self.local_send_time)
            ZoneInfo(self.timezone_name)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise AccountingValidationError(
                "report timezone or local send time is invalid"
            ) from exc
        if parsed_time.tzinfo is not None:
            raise AccountingValidationError("local send time cannot include timezone")


@dataclass(frozen=True)
class ImportedInvestorSnapshot:
    record_id: str
    investor_id: str
    occurred_at: datetime
    units: Decimal
    equity: Decimal
    source_note: str

    def __post_init__(self) -> None:
        require_identifier(self.record_id, "record_id")
        require_identifier(self.investor_id, "investor_id")
        require_identifier(self.source_note, "source_note")
        require_utc(self.occurred_at, "imported snapshot occurred_at")
        _require_units(self.units, "imported snapshot units")
        require_money(self.equity, "imported snapshot equity", positive=True)


@dataclass(frozen=True)
class SharedPoolBootstrap:
    migration_id: str
    pool_id: str
    strategy_id: str
    strategy_version: str
    occurred_at: datetime
    exchange_equity: Decimal
    investors: tuple[OpeningInvestor, ...]
    imported_snapshots: tuple[ImportedInvestorSnapshot, ...] = ()
    actor: str = "operator"
    reason: str = "audited opening balance migration"

    def __post_init__(self) -> None:
        for field in (
            "migration_id",
            "pool_id",
            "strategy_id",
            "strategy_version",
            "actor",
            "reason",
        ):
            require_identifier(str(getattr(self, field)), field)
        require_utc(self.occurred_at, "bootstrap occurred_at")
        require_money(self.exchange_equity, "exchange equity", positive=True)
        if len(self.investors) < 2:
            raise AccountingValidationError(
                "shared pool bootstrap requires at least two investors"
            )
        ids = [item.investor_id for item in self.investors]
        emails = [item.email.strip().casefold() for item in self.investors]
        if len(ids) != len(set(ids)) or len(emails) != len(set(emails)):
            raise AccountingValidationError("investor identities must be unique")
        if any(item.investor_id not in set(ids) for item in self.imported_snapshots):
            raise AccountingValidationError("imported snapshot investor is unknown")
        if any(item.occurred_at > self.occurred_at for item in self.imported_snapshots):
            raise AccountingValidationError(
                "imported snapshot cannot occur after the bootstrap cutover"
            )


@dataclass(frozen=True)
class BootstrapResult:
    pool_id: str
    reconciliation_id: str
    snapshot_id: str
    pool_equity: Decimal
    outstanding_units: Decimal
    unit_nav: Decimal


def bootstrap_shared_pool(
    store: SQLiteAccountingStore, spec: SharedPoolBootstrap
) -> BootstrapResult:
    """Atomically establish an audited opening position in an empty ledger."""

    event = SourceEvent(
        event_id=f"bootstrap:{spec.migration_id}",
        event_type="MIGRATION_OPENING",
        occurred_at=spec.occurred_at,
        payload={
            "pool_id": spec.pool_id,
            "strategy_id": spec.strategy_id,
            "strategy_version": spec.strategy_version,
            "exchange_equity": str(spec.exchange_equity),
            "investors": "|".join(
                f"{item.investor_id}:{item.opening_contribution}:{item.opening_units}"
                for item in spec.investors
            ),
            "imported_snapshot_count": str(len(spec.imported_snapshots)),
        },
    )
    prior = store.event_result(event)
    if prior is not None:
        return BootstrapResult(
            pool_id=spec.pool_id,
            reconciliation_id=prior["reconciliation_id"],
            snapshot_id=prior["snapshot_id"],
            pool_equity=Decimal(prior["pool_equity"]),
            outstanding_units=Decimal(prior["outstanding_units"]),
            unit_nav=Decimal(prior["unit_nav"]),
        )

    contributions = sum(
        (item.opening_contribution for item in spec.investors), Decimal("0")
    )
    units = sum((item.opening_units for item in spec.investors), Decimal("0"))
    performance = spec.exchange_equity - contributions
    postings: list[Posting] = [
        Posting(
            chart.pool_cash(spec.pool_id), EntryDirection.DEBIT, spec.exchange_equity
        )
    ]
    postings.extend(
        Posting(
            chart.investor_capital(item.investor_id),
            EntryDirection.CREDIT,
            item.opening_contribution,
        )
        for item in spec.investors
    )
    if performance > 0:
        postings.append(
            Posting(
                chart.realized_pnl(spec.pool_id),
                EntryDirection.CREDIT,
                performance,
            )
        )
    elif performance < 0:
        postings.append(
            Posting(
                chart.realized_pnl(spec.pool_id),
                EntryDirection.DEBIT,
                -performance,
            )
        )

    reconciliation_id = f"bootstrap-recon:{spec.migration_id}"
    snapshot = value_pool(
        snapshot_id=f"bootstrap-snapshot:{spec.migration_id}",
        pool_id=spec.pool_id,
        occurred_at=spec.occurred_at,
        cash=spec.exchange_equity,
        unrealized_pnl=Decimal("0"),
        outstanding_units=units,
        reconciliation_id=reconciliation_id,
    )
    transaction = LedgerTransaction(
        transaction_id=f"bootstrap-ledger:{spec.migration_id}",
        kind="MIGRATION_OPENING",
        occurred_at=spec.occurred_at,
        actor=spec.actor,
        reason=spec.reason,
        external_reference=spec.migration_id,
        postings=tuple(postings),
    )
    reconciliation = ReconciliationResult(
        reconciliation_id=reconciliation_id,
        occurred_at=spec.occurred_at,
        exchange_equity=spec.exchange_equity,
        internal_equity=spec.exchange_equity,
        difference=Decimal("0"),
        status=ReconciliationStatus.PASSED,
        reasons=(),
    )

    try:
        with store.transaction() as connection:
            occupied = connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM investors) +
                  (SELECT COUNT(*) FROM strategy_pools) +
                  (SELECT COUNT(*) FROM ledger_transactions)
                """
            ).fetchone()[0]
            if int(occupied) != 0:
                raise AccountingValidationError(
                    "opening migration requires an empty business ledger"
                )
            connection.execute(
                """
                INSERT INTO strategy_pools(
                    pool_id, strategy_id, strategy_version, base_asset,
                    initial_unit_nav, created_at
                ) VALUES (?, ?, ?, 'USDT', '1', ?)
                """,
                (
                    spec.pool_id,
                    spec.strategy_id,
                    spec.strategy_version,
                    spec.occurred_at.isoformat(),
                ),
            )
            for item in spec.investors:
                connection.execute(
                    "INSERT INTO investors(investor_id, display_name, created_at) VALUES (?, ?, ?)",
                    (item.investor_id, item.display_name, spec.occurred_at.isoformat()),
                )
                connection.execute(
                    """
                    INSERT INTO verified_email_addresses(
                        email, investor_id, verified, created_at
                    ) VALUES (?, ?, 1, ?)
                    """,
                    (
                        item.email.strip().casefold(),
                        item.investor_id,
                        spec.occurred_at.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO report_preferences(
                        investor_id, frequency, timezone, local_send_time,
                        monthly_send_day, enabled
                    ) VALUES (?, ?, ?, ?, ?, 1)
                    """,
                    (
                        item.investor_id,
                        item.frequency.value,
                        item.timezone_name,
                        item.local_send_time,
                        item.monthly_send_day,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO subscriptions(
                        investor_id, pool_id, units, total_contributions,
                        total_withdrawals, active, updated_at
                    ) VALUES (?, ?, ?, ?, '0', 1, ?)
                    """,
                    (
                        item.investor_id,
                        spec.pool_id,
                        str(item.opening_units),
                        str(item.opening_contribution),
                        spec.occurred_at.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO unit_lots(
                        lot_id, investor_id, pool_id, event_id, units,
                        amount, lot_type, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'ISSUE', ?)
                    """,
                    (
                        f"bootstrap-lot:{spec.migration_id}:{item.investor_id}",
                        item.investor_id,
                        spec.pool_id,
                        f"bootstrap-lot-event:{spec.migration_id}:{item.investor_id}",
                        str(item.opening_units),
                        str(item.opening_contribution),
                        spec.occurred_at.isoformat(),
                    ),
                )
            store.insert_source_event(connection, event)
            store.insert_transaction(connection, event.event_id, transaction)
            store.insert_reconciliation(connection, reconciliation)
            store.insert_snapshot(connection, snapshot)
            for imported in spec.imported_snapshots:
                nav = (imported.equity / imported.units).quantize(UNIT_QUANTUM)
                connection.execute(
                    """
                    INSERT INTO imported_investor_snapshots(
                        record_id, investor_id, pool_id, occurred_at,
                        units, equity, unit_nav, source_note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        imported.record_id,
                        imported.investor_id,
                        spec.pool_id,
                        imported.occurred_at.isoformat(),
                        str(imported.units),
                        str(imported.equity.quantize(MONEY_QUANTUM)),
                        str(nav),
                        imported.source_note,
                    ),
                )
            store.complete_source_event(
                connection,
                event.event_id,
                {
                    "reconciliation_id": reconciliation_id,
                    "snapshot_id": snapshot.snapshot_id,
                    "pool_equity": str(snapshot.pool_equity),
                    "outstanding_units": str(snapshot.outstanding_units),
                    "unit_nav": str(snapshot.unit_nav),
                },
                spec.occurred_at,
            )
    except sqlite3.IntegrityError:
        prior = store.event_result(event)
        if prior is None:
            raise
        return BootstrapResult(
            pool_id=spec.pool_id,
            reconciliation_id=prior["reconciliation_id"],
            snapshot_id=prior["snapshot_id"],
            pool_equity=Decimal(prior["pool_equity"]),
            outstanding_units=Decimal(prior["outstanding_units"]),
            unit_nav=Decimal(prior["unit_nav"]),
        )
    return BootstrapResult(
        pool_id=spec.pool_id,
        reconciliation_id=reconciliation_id,
        snapshot_id=snapshot.snapshot_id,
        pool_equity=snapshot.pool_equity,
        outstanding_units=snapshot.outstanding_units,
        unit_nav=snapshot.unit_nav,
    )
