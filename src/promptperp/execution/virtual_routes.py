from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from promptperp.domain import PositionSide


class VirtualFillRouteError(RuntimeError):
    pass


class VirtualFillRole(str, Enum):
    OPEN = "OPEN"
    REDUCE = "REDUCE"


@dataclass(frozen=True)
class VirtualFillRoute:
    strategy_id: str
    run_id: str
    symbol: str
    position_side: PositionSide
    exchange_order_id: str
    role: VirtualFillRole

    def __post_init__(self) -> None:
        if not all(
            (self.strategy_id, self.run_id, self.symbol, self.exchange_order_id)
        ):
            raise ValueError("virtual fill route identity is required")
        if self.symbol != self.symbol.upper():
            raise ValueError("virtual fill route symbol must be uppercase")


@dataclass(frozen=True)
class VirtualFillOwner:
    strategy_id: str
    run_id: str
    symbol: str
    position_side: PositionSide
    role: VirtualFillRole

    def __post_init__(self) -> None:
        if not all((self.strategy_id, self.run_id, self.symbol)):
            raise ValueError("virtual fill owner identity is required")
        if self.symbol != self.symbol.upper():
            raise ValueError("virtual fill owner symbol must be uppercase")


class VirtualFillRouteRegistry:
    """Durably bind one exchange order to exactly one virtual strategy lot."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS virtual_fill_routes (
                    client_order_id TEXT PRIMARY KEY,
                    exchange_order_id TEXT,
                    strategy_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    position_side TEXT NOT NULL CHECK (
                        position_side IN ('LONG', 'SHORT')
                    ),
                    role TEXT NOT NULL CHECK (role IN ('OPEN', 'REDUCE')),
                    UNIQUE(symbol, exchange_order_id)
                )
                """
            )
        os.chmod(self.path, 0o600)

    def prepare(
        self,
        *,
        strategy_id: str,
        run_id: str,
        symbol: str,
        position_side: PositionSide,
        client_order_id: str,
        role: VirtualFillRole,
    ) -> None:
        if not all((strategy_id, run_id, symbol, client_order_id)):
            raise ValueError("virtual fill route identity is required")
        if symbol != symbol.upper():
            raise ValueError("virtual fill route symbol must be uppercase")
        payload = (
            strategy_id,
            run_id,
            symbol,
            position_side.value,
            role.value,
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT strategy_id, run_id, symbol, position_side, role
                FROM virtual_fill_routes WHERE client_order_id = ?
                """,
                (client_order_id,),
            ).fetchone()
            if row is not None:
                if tuple(row) != payload:
                    raise VirtualFillRouteError(
                        "client order ID has conflicting virtual ownership"
                    )
                return
            connection.execute(
                """
                INSERT INTO virtual_fill_routes(
                    client_order_id, strategy_id, run_id, symbol,
                    position_side, role
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (client_order_id, *payload),
            )

    def confirm(self, *, client_order_id: str, exchange_order_id: str) -> None:
        if not client_order_id or not exchange_order_id:
            raise ValueError("client and exchange order identities are required")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT exchange_order_id FROM virtual_fill_routes
                WHERE client_order_id = ?
                """,
                (client_order_id,),
            ).fetchone()
            if row is None:
                raise VirtualFillRouteError(
                    "exchange order confirmation has no prepared virtual owner"
                )
            current = row["exchange_order_id"]
            if current is not None and str(current) != exchange_order_id:
                raise VirtualFillRouteError(
                    "client order ID resolved to conflicting exchange orders"
                )
            try:
                connection.execute(
                    """
                    UPDATE virtual_fill_routes SET exchange_order_id = ?
                    WHERE client_order_id = ?
                    """,
                    (exchange_order_id, client_order_id),
                )
            except sqlite3.IntegrityError as exc:
                raise VirtualFillRouteError(
                    "exchange order already belongs to another virtual owner"
                ) from exc

    def route_for_order(
        self, exchange_order_id: str, *, symbol: str | None = None
    ) -> VirtualFillRoute | None:
        if not exchange_order_id:
            raise ValueError("exchange order identity is required")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM virtual_fill_routes WHERE exchange_order_id = ?
                AND (? IS NULL OR symbol = ?)
                """,
                (exchange_order_id, symbol, symbol),
            ).fetchall()
        if len(rows) > 1:
            raise VirtualFillRouteError("exchange order lookup requires symbol scope")
        if not rows:
            return None
        row = rows[0]
        return VirtualFillRoute(
            strategy_id=str(row["strategy_id"]),
            run_id=str(row["run_id"]),
            symbol=str(row["symbol"]),
            position_side=PositionSide(str(row["position_side"])),
            exchange_order_id=str(row["exchange_order_id"]),
            role=VirtualFillRole(str(row["role"])),
        )

    def owner_for_client(self, client_order_id: str) -> VirtualFillOwner | None:
        """Resolve prepared ownership before an exchange order ID is known."""

        if not client_order_id:
            raise ValueError("client order identity is required")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT strategy_id, run_id, symbol, position_side, role
                FROM virtual_fill_routes WHERE client_order_id = ?
                """,
                (client_order_id,),
            ).fetchone()
        if row is None:
            return None
        return VirtualFillOwner(
            strategy_id=str(row["strategy_id"]),
            run_id=str(row["run_id"]),
            symbol=str(row["symbol"]),
            position_side=PositionSide(str(row["position_side"])),
            role=VirtualFillRole(str(row["role"])),
        )

    def symbols(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT symbol FROM virtual_fill_routes ORDER BY symbol"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection
