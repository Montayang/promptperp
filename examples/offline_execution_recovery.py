"""Synthetic recovery wiring: no exchange client, credentials, orders or email."""

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from promptperp.domain import FuturesPosition, PositionSide
from promptperp.execution.confirmation import FillConfirmation
from promptperp.operations.account_turn import SharedAccountTurn
from promptperp.operations.request_budget import RequestBudget
from promptperp.operations.settlement_gate import SettlementGate, ValuationPending


def main() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        budget = RequestBudget(root / "budget.db", capacity=100, window_seconds=60)
        turn = SharedAccountTurn(root / "turn")
        gate = SettlementGate(root / "settlement.db", max_wait_seconds=10)
        key = ("BTCUSDT", PositionSide.LONG)
        owned = {key: Decimal(1)}
        attempts = []  # Production integrations must persist this audit.
        # Reserve before the lock, including retry and final reconciliation reads.
        permit = budget.reserve(20)

        def positions() -> tuple[FuturesPosition, ...]:
            permit.consume(1)
            return (FuturesPosition(*key, Decimal(2), Decimal(100)),)

        def refresh(symbols: frozenset[str]) -> None:
            permit.consume(2)
            assert symbols == frozenset({"BTCUSDT"})
            owned[key] = Decimal(2)

        with turn.acquire():
            FillConfirmation(
                read_positions=positions,
                read_owned=lambda: owned,
                refresh=refresh,
                audit=attempts.append,
                sleep=lambda _: None,
            ).confirm(before={key: Decimal(1)}, expected={key: Decimal(2)})

            def lagging_settlement() -> None:
                permit.consume(2)
                raise ValuationPending("synthetic settlement batch not complete")

            try:
                gate.reconcile(lagging_settlement)
            except ValuationPending:
                pass  # No trading; next poll must be admitted outside the lock.

        permit = budget.reserve(20)
        with turn.acquire():
            gate.reconcile(lambda: permit.consume(2))  # Synthetic full verification.
        assert len(attempts) == 2 and not attempts[-1].differences
        print("offline recovery verified; execution_permitted=false")


if __name__ == "__main__":
    main()
