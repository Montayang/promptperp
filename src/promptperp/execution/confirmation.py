"""Read-only convergence checks for a transition proven by confirmed fills."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Mapping

from promptperp.domain import FuturesPosition, PositionSide, ReconciliationFailed

Quantities = Mapping[tuple[str, PositionSide], Decimal]


@dataclass(frozen=True)
class QuantityDifference:
    symbol: str
    side: PositionSide
    before: Decimal
    expected: Decimal
    owned: Decimal
    physical: Decimal


@dataclass(frozen=True)
class ConfirmationAttempt:
    number: int
    differences: tuple[QuantityDifference, ...]
    explainable: bool


class FillConfirmation:
    """Caller holds the shared account barrier and supplies a durable audit sink.

    ``expected`` must come from confirmed cumulative fills, never target orders.
    All callbacks are read-only except ledger refresh / audit persistence. Each
    transport read needs its own timeout; the deadline bounds retry admission,
    not an already-running callback. No amount tolerance is applied.
    """

    def __init__(
        self,
        *,
        read_positions: Callable[[], tuple[FuturesPosition, ...]],
        read_owned: Callable[[], Quantities],
        refresh: Callable[[frozenset[str]], None],
        audit: Callable[[ConfirmationAttempt], None],
        retry_delays: tuple[float, ...] = (0.25, 0.75),
        deadline_seconds: float = 5,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if (
            not math.isfinite(deadline_seconds)
            or deadline_seconds <= 0
            or len(retry_delays) > 10
            or any(not math.isfinite(d) or d < 0 for d in retry_delays)
        ):
            raise ValueError("confirmation retry policy must be finite and bounded")
        self.read_positions, self.read_owned = read_positions, read_owned
        self.refresh, self.audit = refresh, audit
        self.delays, self.deadline = retry_delays, deadline_seconds
        self.sleep, self.monotonic = sleep, monotonic

    def confirm(self, *, before: Quantities, expected: Quantities) -> None:
        before, expected = dict(before), dict(expected)
        self._validate(before)
        self._validate(expected)
        # Keep this scope immutable. A refresh can clear its own dirty set even
        # when exchange trade history has not caught up with the order response.
        affected = frozenset(
            symbol
            for symbol, side in set(before) | set(expected)
            if before.get((symbol, side), Decimal(0))
            != expected.get((symbol, side), Decimal(0))
        )
        started = self.monotonic()
        for attempt in range(len(self.delays) + 1):
            if attempt:
                delay = self.delays[attempt - 1]
                if self.monotonic() - started + delay >= self.deadline:
                    raise ReconciliationFailed("fill confirmation deadline expired")
                self.sleep(delay)
                if self.monotonic() - started >= self.deadline:
                    raise ReconciliationFailed("fill confirmation deadline expired")
                self.refresh(affected)
            owned = dict(self.read_owned())
            self._validate(owned)
            physical: dict[tuple[str, PositionSide], Decimal] = {}
            for position in self.read_positions():
                key = (position.symbol, position.side)
                if key in physical:
                    raise ReconciliationFailed("duplicate physical position row")
                physical[key] = position.quantity
            self._validate(physical)
            differences = []
            explainable = True
            keys = set(before) | set(expected) | set(owned) | set(physical)
            for key in sorted(keys, key=lambda k: (k[0], k[1].value)):
                old, want, local, actual = (
                    values.get(key, Decimal(0))
                    for values in (before, expected, owned, physical)
                )
                if local == actual == want:
                    continue
                differences.append(QuantityDifference(*key, old, want, local, actual))
                if old == want or not (
                    min(old, want) <= local <= max(old, want)
                    and min(old, want) <= actual <= max(old, want)
                ):
                    explainable = False
            self.audit(
                ConfirmationAttempt(attempt + 1, tuple(differences), explainable)
            )
            if not differences:
                return
            if not explainable or attempt == len(self.delays):
                raise ReconciliationFailed(
                    "physical/owned quantities are not confirmed"
                )

    @staticmethod
    def _validate(values: Quantities) -> None:
        for (symbol, side), quantity in values.items():
            if (
                not symbol
                or not isinstance(side, PositionSide)
                or not quantity.is_finite()
                or quantity < 0
            ):
                raise ReconciliationFailed("invalid position quantity or identity")
