from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Mapping

from promptperp.accounting.allocation import issue_units, redeem_units
from promptperp.accounting.attribution import require_owned_settlement
from promptperp.accounting.errors import (
    AccountingValidationError,
    ReconciliationBlocked,
)
from promptperp.accounting.ledger import (
    contribution_transaction,
    settlement_transaction,
    withdrawal_transaction,
)
from promptperp.accounting.models import (
    CashFlowGate,
    InvestorView,
    ReconciliationResult,
    ReconciliationStatus,
    ReportFrequency,
    SourceEvent,
    TradingSettlement,
    ValuationSnapshot,
)
from promptperp.accounting.reconciliation import reconcile, require_cash_flow_window
from promptperp.accounting.valuation import value_pool
from promptperp.storage.accounting_sqlite import SQLiteAccountingStore


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def _require_gate_snapshot(gate: CashFlowGate, snapshot: sqlite3.Row) -> None:
    if gate.reconciliation_id != str(
        snapshot["reconciliation_id"]
    ) or gate.reconciled_at != datetime.fromisoformat(str(snapshot["occurred_at"])):
        raise ReconciliationBlocked(
            "cash-flow gate does not match the valuation reconciliation"
        )


class InvestorAccountingService:
    def __init__(self, store: SQLiteAccountingStore):
        self.store = store

    def initialize(self) -> None:
        self.store.initialize()

    def register_investor(
        self,
        *,
        investor_id: str,
        display_name: str,
        email: str,
        frequency: ReportFrequency,
        timezone_name: str,
        local_send_time: str,
        monthly_send_day: int = 1,
        occurred_at: datetime,
    ) -> None:
        if not 1 <= monthly_send_day <= 28:
            raise AccountingValidationError("monthly send day must be between 1 and 28")
        self.store.register_investor(
            investor_id=investor_id,
            display_name=display_name,
            email=email,
            frequency=frequency,
            timezone_name=timezone_name,
            local_send_time=local_send_time,
            monthly_send_day=monthly_send_day,
            occurred_at=occurred_at,
        )

    def create_pool(
        self,
        *,
        pool_id: str,
        strategy_id: str,
        strategy_version: str,
        occurred_at: datetime,
        initial_unit_nav: Decimal = Decimal("1"),
    ) -> None:
        self.store.create_pool(
            pool_id=pool_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            initial_unit_nav=initial_unit_nav,
            occurred_at=occurred_at,
        )

    def contribute(
        self,
        *,
        event_id: str,
        investor_id: str,
        pool_id: str,
        amount: Decimal,
        gate: CashFlowGate,
        actor: str,
        reason: str,
        external_reference: str,
        occurred_at: datetime,
    ) -> Decimal:
        require_cash_flow_window(gate)
        event = SourceEvent(
            event_id=event_id,
            event_type="CONTRIBUTION",
            occurred_at=occurred_at,
            payload={
                "investor_id": investor_id,
                "pool_id": pool_id,
                "amount": str(amount),
                "actor": actor,
                "reason": reason,
                "external_reference": external_reference,
            },
        )
        prior = self.store.event_result(event)
        if prior is not None:
            return _d(prior["units"])
        transaction = contribution_transaction(
            transaction_id=f"cashflow:{event_id}",
            investor_id=investor_id,
            pool_id=pool_id,
            amount=amount,
            occurred_at=occurred_at,
            actor=actor,
            reason=reason,
            external_reference=external_reference,
        )
        try:
            with self.store.transaction() as connection:
                pool = connection.execute(
                    "SELECT * FROM strategy_pools WHERE pool_id = ? AND active = 1",
                    (pool_id,),
                ).fetchone()
                investor = connection.execute(
                    "SELECT investor_id FROM investors WHERE investor_id = ? AND active = 1",
                    (investor_id,),
                ).fetchone()
                if pool is None or investor is None:
                    raise AccountingValidationError(
                        "investor or strategy pool is inactive"
                    )
                current_units = self.store.pool_units(connection, pool_id)
                if current_units == 0:
                    unit_nav = _d(pool["initial_unit_nav"])
                else:
                    snapshot = self.store.latest_snapshot(connection, pool_id)
                    if snapshot is None:
                        raise ReconciliationBlocked(
                            "existing pool has no reconciled valuation snapshot"
                        )
                    _require_gate_snapshot(gate, snapshot)
                    unit_nav = _d(snapshot["unit_nav"])
                issued = issue_units(amount, unit_nav)
                subscription = connection.execute(
                    """
                    SELECT * FROM subscriptions
                    WHERE investor_id = ? AND pool_id = ?
                    """,
                    (investor_id, pool_id),
                ).fetchone()
                self.store.insert_source_event(connection, event)
                self.store.insert_transaction(connection, event_id, transaction)
                if subscription is None:
                    connection.execute(
                        """
                        INSERT INTO subscriptions(
                            investor_id, pool_id, units, total_contributions,
                            total_withdrawals, active, updated_at
                        ) VALUES (?, ?, ?, ?, '0', 1, ?)
                        """,
                        (
                            investor_id,
                            pool_id,
                            str(issued),
                            str(amount),
                            occurred_at.isoformat(),
                        ),
                    )
                else:
                    updated_units = _d(subscription["units"]) + issued
                    contributions = _d(subscription["total_contributions"]) + amount
                    connection.execute(
                        """
                        UPDATE subscriptions SET units = ?, total_contributions = ?,
                            active = 1, updated_at = ?
                        WHERE investor_id = ? AND pool_id = ?
                        """,
                        (
                            str(updated_units),
                            str(contributions),
                            occurred_at.isoformat(),
                            investor_id,
                            pool_id,
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
                        f"lot:{event_id}",
                        investor_id,
                        pool_id,
                        event_id,
                        str(issued),
                        str(amount),
                        occurred_at.isoformat(),
                    ),
                )
                self.store.complete_source_event(
                    connection, event_id, {"units": str(issued)}, occurred_at
                )
        except sqlite3.IntegrityError:
            prior = self.store.event_result(event)
            if prior is None:
                raise
            return _d(prior["units"])
        return issued

    def withdraw(
        self,
        *,
        event_id: str,
        investor_id: str,
        pool_id: str | None = None,
        amount: Decimal,
        gate: CashFlowGate,
        actor: str,
        reason: str,
        external_reference: str,
        occurred_at: datetime,
    ) -> Decimal:
        require_cash_flow_window(gate)
        event = SourceEvent(
            event_id=event_id,
            event_type="WITHDRAWAL",
            occurred_at=occurred_at,
            payload={
                "investor_id": investor_id,
                "pool_id": "" if pool_id is None else pool_id,
                "amount": str(amount),
                "actor": actor,
                "reason": reason,
                "external_reference": external_reference,
            },
        )
        prior = self.store.event_result(event)
        if prior is not None:
            return _d(prior["units"])
        try:
            with self.store.transaction() as connection:
                subscriptions = connection.execute(
                    """
                    SELECT * FROM subscriptions
                    WHERE investor_id = ? AND active = 1
                      AND (? IS NULL OR pool_id = ?)
                    ORDER BY pool_id
                    """,
                    (investor_id, pool_id, pool_id),
                ).fetchall()
                if not subscriptions:
                    raise AccountingValidationError(
                        "investor has no active subscription"
                    )
                if len(subscriptions) != 1:
                    raise AccountingValidationError(
                        "pool_id is required for a multi-pool withdrawal"
                    )
                subscription = subscriptions[0]
                selected_pool_id = str(subscription["pool_id"])
                snapshot = self.store.latest_snapshot(connection, selected_pool_id)
                if snapshot is None:
                    raise ReconciliationBlocked(
                        "withdrawal needs a reconciled valuation"
                    )
                _require_gate_snapshot(gate, snapshot)
                redeemed = redeem_units(
                    amount, _d(snapshot["unit_nav"]), _d(subscription["units"])
                )
                transaction = withdrawal_transaction(
                    transaction_id=f"cashflow:{event_id}",
                    investor_id=investor_id,
                    pool_id=selected_pool_id,
                    amount=amount,
                    occurred_at=occurred_at,
                    actor=actor,
                    reason=reason,
                    external_reference=external_reference,
                )
                remaining = _d(subscription["units"]) - redeemed
                withdrawals = _d(subscription["total_withdrawals"]) + amount
                self.store.insert_source_event(connection, event)
                self.store.insert_transaction(connection, event_id, transaction)
                connection.execute(
                    """
                    UPDATE subscriptions SET units = ?, total_withdrawals = ?,
                        active = ?, updated_at = ?
                    WHERE investor_id = ? AND pool_id = ?
                    """,
                    (
                        str(remaining),
                        str(withdrawals),
                        int(remaining > 0),
                        occurred_at.isoformat(),
                        investor_id,
                        selected_pool_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO unit_lots(
                        lot_id, investor_id, pool_id, event_id, units,
                        amount, lot_type, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'REDEEM', ?)
                    """,
                    (
                        f"lot:{event_id}",
                        investor_id,
                        selected_pool_id,
                        event_id,
                        str(-redeemed),
                        str(-amount),
                        occurred_at.isoformat(),
                    ),
                )
                self.store.complete_source_event(
                    connection, event_id, {"units": str(redeemed)}, occurred_at
                )
        except sqlite3.IntegrityError:
            prior = self.store.event_result(event)
            if prior is None:
                raise
            return _d(prior["units"])
        return redeemed

    def record_settlement(self, settlement: TradingSettlement) -> Decimal:
        pool = self.store.get_pool_for_strategy(settlement.strategy_id)
        require_owned_settlement(
            settlement, expected_strategy_id=str(pool["strategy_id"])
        )
        event = SourceEvent(
            event_id=settlement.event_id,
            event_type="TRADING_SETTLEMENT",
            occurred_at=settlement.occurred_at,
            payload={
                "strategy_id": settlement.strategy_id,
                "run_id": settlement.run_id,
                "intent_id": settlement.intent_id,
                "order_id": settlement.order_id,
                "trade_id": settlement.trade_id,
                "realized_pnl": str(settlement.realized_pnl),
                "commission": str(settlement.commission),
                "funding": str(settlement.funding),
                "asset": settlement.asset,
            },
        )
        prior = self.store.event_result(event)
        if prior is not None:
            return _d(prior["net_change"])
        transaction = settlement_transaction(
            pool_id=str(pool["pool_id"]), settlement=settlement
        )
        try:
            with self.store.transaction() as connection:
                self.store.insert_source_event(connection, event)
                self.store.insert_transaction(connection, event.event_id, transaction)
                self.store.complete_source_event(
                    connection,
                    event.event_id,
                    {"net_change": str(settlement.net_change)},
                    settlement.occurred_at,
                )
        except sqlite3.IntegrityError:
            prior = self.store.event_result(event)
            if prior is None:
                raise
            return _d(prior["net_change"])
        return settlement.net_change

    def reconcile_and_value(
        self,
        *,
        reconciliation_id: str,
        occurred_at: datetime,
        exchange_equity: Decimal,
        unrealized_pnl: Mapping[str, Decimal],
        blocking_reasons: tuple[str, ...] = (),
    ) -> tuple[ReconciliationResult, tuple[ValuationSnapshot, ...]]:
        snapshots: list[ValuationSnapshot] = []
        with self.store.transaction() as connection:
            pools = self.store.list_active_pools(connection)
            pool_ids = {str(pool["pool_id"]) for pool in pools}
            if set(unrealized_pnl) != pool_ids:
                raise AccountingValidationError(
                    "unrealized PnL must cover every active strategy pool exactly"
                )
            internal_equity = sum(
                (
                    self.store.pool_cash(connection, pool_id) + unrealized_pnl[pool_id]
                    for pool_id in sorted(pool_ids)
                ),
                Decimal("0"),
            )
            result = reconcile(
                reconciliation_id=reconciliation_id,
                occurred_at=occurred_at,
                exchange_equity=exchange_equity,
                internal_equity=internal_equity,
                blocking_reasons=blocking_reasons,
            )
            self.store.insert_reconciliation(connection, result)
            if result.status is ReconciliationStatus.PASSED:
                for pool_id in sorted(pool_ids):
                    units = self.store.pool_units(connection, pool_id)
                    if units <= 0:
                        continue
                    snapshot = value_pool(
                        snapshot_id=f"snapshot:{reconciliation_id}:{pool_id}",
                        pool_id=pool_id,
                        occurred_at=occurred_at,
                        cash=self.store.pool_cash(connection, pool_id),
                        unrealized_pnl=unrealized_pnl[pool_id],
                        outstanding_units=units,
                        reconciliation_id=reconciliation_id,
                    )
                    self.store.insert_snapshot(connection, snapshot)
                    snapshots.append(snapshot)
        return result, tuple(snapshots)

    def investor_view(self, investor_id: str) -> InvestorView:
        return self.store.investor_view_by_id(investor_id)

    def investor_view_for_email(self, email: str) -> InvestorView | None:
        investor_id = self.store.investor_id_for_email(email)
        if investor_id is None:
            return None
        return self.store.investor_view_by_id(investor_id)

    def health(self) -> Mapping[str, str]:
        self.store.integrity_check()
        event_count = self.store.audit_ledger()
        return {"status": "healthy", "ledger_transactions": str(event_count)}
