from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from pathlib import Path
from typing import Iterator, Mapping, cast

from promptperp.domain import FuturesPosition, PositionSide

MONEY_QUANTUM = Decimal("0.00000001")


class VirtualPositionError(RuntimeError):
    pass


class ExchangePositionMode(str, Enum):
    """How Binance represents physical exposure for one symbol."""

    HEDGE = "HEDGE"
    ONE_WAY = "ONE_WAY"


@dataclass(frozen=True)
class VirtualPosition:
    strategy_id: str
    run_id: str
    symbol: str
    position_side: PositionSide
    quantity: Decimal
    average_entry_price: Decimal
    realized_pnl: Decimal
    commission: Decimal
    funding: Decimal

    @property
    def key(self) -> tuple[str, str, str, PositionSide]:
        return self.strategy_id, self.run_id, self.symbol, self.position_side


@dataclass(frozen=True)
class VirtualSettlement:
    realized_pnl: Decimal
    commission: Decimal
    funding: Decimal

    @property
    def net_amount(self) -> Decimal:
        return self.realized_pnl - self.commission + self.funding


class VirtualPositionStore:
    """Allocate fungible exchange positions to strategy-owned virtual lots."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS virtual_positions (
                    strategy_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    position_side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    entry_notional TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    commission TEXT NOT NULL,
                    funding TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(strategy_id, run_id, symbol, position_side)
                );
                CREATE TABLE IF NOT EXISTS virtual_position_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS virtual_funding_allocations (
                    event_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    PRIMARY KEY(event_id, strategy_id, run_id)
                );
                """
            )
        os.chmod(self.path, 0o600)

    def open_fill(
        self,
        *,
        event_id: str,
        strategy_id: str,
        run_id: str,
        symbol: str,
        position_side: PositionSide,
        quantity: Decimal,
        price: Decimal,
        commission: Decimal,
        occurred_at: datetime,
    ) -> VirtualPosition:
        self._validate_fill(
            event_id,
            strategy_id,
            run_id,
            symbol,
            quantity,
            price,
            commission,
            occurred_at,
        )
        payload = {
            "strategy_id": strategy_id,
            "run_id": run_id,
            "symbol": symbol,
            "position_side": position_side.value,
            "quantity": str(quantity),
            "price": str(price),
            "commission": str(commission),
        }
        with self._transaction() as connection:
            replay = self._claim_event(
                connection, event_id, "OPEN_FILL", payload, occurred_at
            )
            if not replay:
                row = self._row(connection, strategy_id, run_id, symbol, position_side)
                old_quantity = Decimal("0") if row is None else Decimal(row["quantity"])
                old_notional = (
                    Decimal("0") if row is None else Decimal(row["entry_notional"])
                )
                realized = Decimal("0") if row is None else Decimal(row["realized_pnl"])
                fees = Decimal("0") if row is None else Decimal(row["commission"])
                funding = Decimal("0") if row is None else Decimal(row["funding"])
                self._upsert(
                    connection,
                    strategy_id=strategy_id,
                    run_id=run_id,
                    symbol=symbol,
                    position_side=position_side,
                    quantity=old_quantity + quantity,
                    entry_notional=old_notional + quantity * price,
                    realized_pnl=realized,
                    commission=fees + commission,
                    funding=funding,
                    occurred_at=occurred_at,
                )
        return self.position(strategy_id, run_id, symbol, position_side)

    def close_fill(
        self,
        *,
        event_id: str,
        strategy_id: str,
        run_id: str,
        symbol: str,
        position_side: PositionSide,
        quantity: Decimal,
        price: Decimal,
        commission: Decimal,
        occurred_at: datetime,
        realized_pnl: Decimal | None = None,
    ) -> VirtualPosition:
        self._validate_fill(
            event_id,
            strategy_id,
            run_id,
            symbol,
            quantity,
            price,
            commission,
            occurred_at,
        )
        if realized_pnl is not None and not realized_pnl.is_finite():
            raise ValueError("realized PnL must be finite")
        payload = {
            "strategy_id": strategy_id,
            "run_id": run_id,
            "symbol": symbol,
            "position_side": position_side.value,
            "quantity": str(quantity),
            "price": str(price),
            "commission": str(commission),
        }
        if realized_pnl is not None:
            payload["realized_pnl"] = str(realized_pnl)
        with self._transaction() as connection:
            replay = self._claim_event(
                connection, event_id, "CLOSE_FILL", payload, occurred_at
            )
            if not replay:
                row = self._row(connection, strategy_id, run_id, symbol, position_side)
                if row is None:
                    raise VirtualPositionError("close ownership is unproven")
                owned = Decimal(row["quantity"])
                if quantity > owned:
                    raise VirtualPositionError("close exceeds strategy-owned quantity")
                entry_notional = Decimal(row["entry_notional"])
                average_entry = entry_notional / owned
                direction = (
                    Decimal("1")
                    if position_side is PositionSide.LONG
                    else Decimal("-1")
                )
                pnl = (
                    (price - average_entry) * quantity * direction
                    if realized_pnl is None
                    else realized_pnl
                )
                remaining = owned - quantity
                remaining_notional = average_entry * remaining
                self._upsert(
                    connection,
                    strategy_id=strategy_id,
                    run_id=run_id,
                    symbol=symbol,
                    position_side=position_side,
                    quantity=remaining,
                    entry_notional=remaining_notional,
                    realized_pnl=Decimal(row["realized_pnl"]) + pnl,
                    commission=Decimal(row["commission"]) + commission,
                    funding=Decimal(row["funding"]),
                    occurred_at=occurred_at,
                )
        return self.position(strategy_id, run_id, symbol, position_side)

    def allocate_funding(
        self,
        *,
        event_id: str,
        symbol: str,
        position_side: PositionSide,
        total_funding: Decimal,
        mark_price: Decimal,
        occurred_at: datetime,
    ) -> Mapping[tuple[str, str], Decimal]:
        if not event_id or occurred_at.tzinfo is None:
            raise ValueError("funding identity and aware time are required")
        if (
            not total_funding.is_finite()
            or not mark_price.is_finite()
            or mark_price <= 0
        ):
            raise ValueError("funding values are invalid")
        payload = {
            "symbol": symbol,
            "position_side": position_side.value,
            "total_funding": str(total_funding),
            "mark_price": str(mark_price),
        }
        allocations: dict[tuple[str, str], Decimal] = {}
        with self._transaction() as connection:
            replay = self._claim_event(
                connection, event_id, "FUNDING", payload, occurred_at
            )
            if replay:
                saved = connection.execute(
                    "SELECT strategy_id, run_id, amount FROM virtual_funding_allocations WHERE event_id=?",
                    (event_id,),
                ).fetchall()
                if not saved:
                    raise VirtualPositionError("funding replay has no saved allocation")
                return {
                    (str(row["strategy_id"]), str(row["run_id"])): Decimal(
                        row["amount"]
                    )
                    for row in saved
                }
            rows = connection.execute(
                """
                SELECT * FROM virtual_positions
                WHERE symbol = ? AND position_side = ? AND quantity != '0'
                ORDER BY strategy_id, run_id
                """,
                (symbol, position_side.value),
            ).fetchall()
            rows = [row for row in rows if Decimal(row["quantity"]) != 0]
            if not rows:
                raise VirtualPositionError("funding has no owned virtual position")
            total_quantity = sum(
                (Decimal(row["quantity"]) for row in rows), Decimal("0")
            )
            assigned = Decimal("0")
            for index, row in enumerate(rows):
                key = (str(row["strategy_id"]), str(row["run_id"]))
                if index == len(rows) - 1:
                    share = total_funding - assigned
                else:
                    share = (
                        total_funding * Decimal(row["quantity"]) / total_quantity
                    ).quantize(MONEY_QUANTUM, rounding=ROUND_DOWN)
                    assigned += share
                allocations[key] = share
                connection.execute(
                    "INSERT INTO virtual_funding_allocations VALUES (?, ?, ?, ?)",
                    (event_id, key[0], key[1], str(share)),
                )
                if not replay:
                    self._upsert(
                        connection,
                        strategy_id=key[0],
                        run_id=key[1],
                        symbol=symbol,
                        position_side=position_side,
                        quantity=Decimal(row["quantity"]),
                        entry_notional=Decimal(row["entry_notional"]),
                        realized_pnl=Decimal(row["realized_pnl"]),
                        commission=Decimal(row["commission"]),
                        funding=Decimal(row["funding"]) + share,
                        occurred_at=occurred_at,
                    )
        return allocations

    def apply_funding_allocation(
        self,
        *,
        event_id: str,
        strategy_id: str,
        run_id: str,
        symbol: str,
        position_side: PositionSide,
        amount: Decimal,
        occurred_at: datetime,
    ) -> None:
        """Apply a proven historical allocation, including to an already closed lot."""
        if not event_id or not amount.is_finite() or occurred_at.tzinfo is None:
            raise ValueError("funding allocation identity is invalid")
        payload = {
            "strategy_id": strategy_id,
            "run_id": run_id,
            "symbol": symbol,
            "position_side": position_side.value,
            "amount": str(amount),
        }
        with self._transaction() as connection:
            replay = self._claim_event(
                connection, event_id, "FUNDING_ALLOCATION", payload, occurred_at
            )
            if replay:
                return
            row = self._row(connection, strategy_id, run_id, symbol, position_side)
            if row is None:
                raise VirtualPositionError("funding allocation has no historical owner")
            self._upsert(
                connection,
                strategy_id=strategy_id,
                run_id=run_id,
                symbol=symbol,
                position_side=position_side,
                quantity=Decimal(row["quantity"]),
                entry_notional=Decimal(row["entry_notional"]),
                realized_pnl=Decimal(row["realized_pnl"]),
                commission=Decimal(row["commission"]),
                funding=Decimal(row["funding"]) + amount,
                occurred_at=occurred_at,
            )

    def reconcile(
        self,
        observed: tuple[FuturesPosition, ...],
        *,
        position_mode: ExchangePositionMode,
        quantity_tolerance: Decimal = Decimal("0"),
    ) -> tuple[str, ...]:
        if quantity_tolerance < 0:
            raise ValueError("quantity tolerance cannot be negative")
        expected = self.physical_quantities(position_mode=position_mode)
        actual = self._project_physical_quantities(
            (
                (position.symbol, position.side, position.quantity)
                for position in observed
            ),
            position_mode=position_mode,
        )
        keys = set(expected) | set(actual)
        return tuple(
            f"{symbol}:{side.value}:expected={expected.get((symbol, side), Decimal('0'))}:actual={actual.get((symbol, side), Decimal('0'))}"
            for symbol, side in sorted(keys, key=lambda item: (item[0], item[1].value))
            if abs(
                expected.get((symbol, side), Decimal("0"))
                - actual.get((symbol, side), Decimal("0"))
            )
            > quantity_tolerance
        )

    def physical_quantities(
        self, *, position_mode: ExchangePositionMode
    ) -> Mapping[tuple[str, PositionSide], Decimal]:
        """Project strategy-owned lots into the account's physical position shape."""

        return self._project_physical_quantities(
            (
                (position.symbol, position.position_side, position.quantity)
                for position in self.positions()
            ),
            position_mode=position_mode,
        )

    def position(
        self,
        strategy_id: str,
        run_id: str,
        symbol: str,
        position_side: PositionSide,
    ) -> VirtualPosition:
        with self._connect() as connection:
            row = self._row(connection, strategy_id, run_id, symbol, position_side)
        if row is None:
            raise VirtualPositionError("virtual position does not exist")
        return self._position(row)

    def positions(self) -> tuple[VirtualPosition, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM virtual_positions WHERE quantity != '0' ORDER BY strategy_id, run_id, symbol, position_side"
            ).fetchall()
        # Decimal strings preserve exchange precision; '0.000' is also flat.
        return tuple(
            self._position(row) for row in rows if Decimal(row["quantity"]) != 0
        )

    def quantities(
        self, *, strategy_id: str, run_id: str
    ) -> Mapping[tuple[str, PositionSide], Decimal]:
        if not strategy_id or not run_id:
            raise ValueError("virtual position ownership is required")
        return {
            (position.symbol, position.position_side): position.quantity
            for position in self.positions()
            if position.strategy_id == strategy_id and position.run_id == run_id
        }

    def settlement(self, strategy_id: str, run_id: str) -> VirtualSettlement:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT realized_pnl, commission, funding FROM virtual_positions
                WHERE strategy_id = ? AND run_id = ?
                """,
                (strategy_id, run_id),
            ).fetchall()
        return VirtualSettlement(
            realized_pnl=sum(
                (Decimal(item["realized_pnl"]) for item in row), Decimal("0")
            ),
            commission=sum((Decimal(item["commission"]) for item in row), Decimal("0")),
            funding=sum((Decimal(item["funding"]) for item in row), Decimal("0")),
        )

    @staticmethod
    def _project_physical_quantities(
        positions: Iterator[tuple[str, PositionSide, Decimal]],
        *,
        position_mode: ExchangePositionMode,
    ) -> dict[tuple[str, PositionSide], Decimal]:
        if not isinstance(position_mode, ExchangePositionMode):
            raise ValueError("exchange position mode must be explicit")
        hedge: dict[tuple[str, PositionSide], Decimal] = {}
        for symbol, side, quantity in positions:
            key = (symbol, side)
            hedge[key] = hedge.get(key, Decimal("0")) + quantity
        hedge = {key: quantity for key, quantity in hedge.items() if quantity != 0}
        if position_mode is ExchangePositionMode.HEDGE:
            return hedge

        signed: dict[str, Decimal] = {}
        for (symbol, side), quantity in hedge.items():
            direction = Decimal("1") if side is PositionSide.LONG else Decimal("-1")
            signed[symbol] = signed.get(symbol, Decimal("0")) + quantity * direction
        projected: dict[tuple[str, PositionSide], Decimal] = {}
        for symbol, quantity in signed.items():
            if quantity > 0:
                projected[(symbol, PositionSide.LONG)] = quantity
            elif quantity < 0:
                projected[(symbol, PositionSide.SHORT)] = abs(quantity)
        return projected

    @staticmethod
    def _validate_fill(
        event_id: str,
        strategy_id: str,
        run_id: str,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        commission: Decimal,
        occurred_at: datetime,
    ) -> None:
        if not all((event_id, strategy_id, run_id, symbol)) or symbol != symbol.upper():
            raise ValueError("fill ownership identity is invalid")
        if occurred_at.tzinfo is None:
            raise ValueError("fill time must be timezone-aware")
        if quantity <= 0 or price <= 0 or commission < 0:
            raise ValueError("fill values are invalid")
        if not all(value.is_finite() for value in (quantity, price, commission)):
            raise ValueError("fill values must be finite")

    @staticmethod
    def _position(row: sqlite3.Row) -> VirtualPosition:
        quantity = Decimal(row["quantity"])
        notional = Decimal(row["entry_notional"])
        return VirtualPosition(
            strategy_id=str(row["strategy_id"]),
            run_id=str(row["run_id"]),
            symbol=str(row["symbol"]),
            position_side=PositionSide(str(row["position_side"])),
            quantity=quantity,
            average_entry_price=Decimal("0") if quantity == 0 else notional / quantity,
            realized_pnl=Decimal(row["realized_pnl"]),
            commission=Decimal(row["commission"]),
            funding=Decimal(row["funding"]),
        )

    @staticmethod
    def _row(
        connection: sqlite3.Connection,
        strategy_id: str,
        run_id: str,
        symbol: str,
        position_side: PositionSide,
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                """
            SELECT * FROM virtual_positions
            WHERE strategy_id = ? AND run_id = ? AND symbol = ? AND position_side = ?
            """,
                (strategy_id, run_id, symbol, position_side.value),
            ).fetchone(),
        )

    @staticmethod
    def _upsert(
        connection: sqlite3.Connection,
        *,
        strategy_id: str,
        run_id: str,
        symbol: str,
        position_side: PositionSide,
        quantity: Decimal,
        entry_notional: Decimal,
        realized_pnl: Decimal,
        commission: Decimal,
        funding: Decimal,
        occurred_at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO virtual_positions(
                strategy_id, run_id, symbol, position_side, quantity,
                entry_notional, realized_pnl, commission, funding, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id, run_id, symbol, position_side) DO UPDATE SET
                quantity=excluded.quantity,
                entry_notional=excluded.entry_notional,
                realized_pnl=excluded.realized_pnl,
                commission=excluded.commission,
                funding=excluded.funding,
                updated_at=excluded.updated_at
            """,
            (
                strategy_id,
                run_id,
                symbol,
                position_side.value,
                str(quantity),
                str(entry_notional),
                str(realized_pnl),
                str(commission),
                str(funding),
                occurred_at.isoformat(),
            ),
        )

    @staticmethod
    def _claim_event(
        connection: sqlite3.Connection,
        event_id: str,
        event_type: str,
        payload: Mapping[str, str],
        occurred_at: datetime,
    ) -> bool:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        row = connection.execute(
            "SELECT event_type, payload_hash FROM virtual_position_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is not None:
            if row["event_type"] != event_type or row["payload_hash"] != digest:
                raise VirtualPositionError("duplicate event identity conflicts")
            return True
        connection.execute(
            """
            INSERT INTO virtual_position_events(
                event_id, event_type, payload_json, payload_hash, occurred_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, event_type, encoded, digest, occurred_at.isoformat()),
        )
        return False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
