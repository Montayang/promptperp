from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from promptperp.strategy_spec import canonical_json

_HEX64 = re.compile(r"[0-9a-f]{64}")


class ApprovalRejected(RuntimeError):
    pass


class ApprovalState(str, Enum):
    REQUESTED = "REQUESTED"
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


class ApprovalDecision(str, Enum):
    GRANT = "GRANT"
    DENY = "DENY"


@dataclass(frozen=True)
class ApprovalBindingV2:
    schema_version: int
    request_id: str
    strategy_id: str
    bundle_fingerprint: str
    spec_fingerprint: str
    evaluation_fingerprint: str
    commit_sha: str
    environment_fingerprint: str
    allocation_policy_fingerprint: str
    risk_policy_fingerprint: str
    requested_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        fingerprints = (
            self.bundle_fingerprint,
            self.spec_fingerprint,
            self.evaluation_fingerprint,
            self.environment_fingerprint,
            self.allocation_policy_fingerprint,
            self.risk_policy_fingerprint,
        )
        if self.schema_version != 2 or not self.request_id or not self.strategy_id:
            raise ValueError("approval binding identity is invalid")
        if not all(_HEX64.fullmatch(value) for value in fingerprints):
            raise ValueError("approval binding fingerprint is invalid")
        if not re.fullmatch(r"[0-9a-f]{40}", self.commit_sha):
            raise ValueError("approval binding commit is invalid")
        if self.requested_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("approval binding timestamps must be timezone-aware")
        if self.expires_at <= self.requested_at:
            raise ValueError("approval expiry must follow request time")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["requested_at"] = self.requested_at.isoformat()
        value["expires_at"] = self.expires_at.isoformat()
        return value

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict())).hexdigest()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ApprovalBindingV2:
        return cls(
            schema_version=value["schema_version"],
            request_id=value["request_id"],
            strategy_id=value["strategy_id"],
            bundle_fingerprint=value["bundle_fingerprint"],
            spec_fingerprint=value["spec_fingerprint"],
            evaluation_fingerprint=value["evaluation_fingerprint"],
            commit_sha=value["commit_sha"],
            environment_fingerprint=value["environment_fingerprint"],
            allocation_policy_fingerprint=value["allocation_policy_fingerprint"],
            risk_policy_fingerprint=value["risk_policy_fingerprint"],
            requested_at=datetime.fromisoformat(value["requested_at"]),
            expires_at=datetime.fromisoformat(value["expires_at"]),
        )


@dataclass(frozen=True)
class ApprovalEvent:
    sequence: int
    request_id: str
    event_type: str
    actor: str
    occurred_at: datetime
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str


class ApprovalLedger:
    def __init__(self, path: str | Path, *, operator_token_digest: str):
        if not _HEX64.fullmatch(operator_token_digest):
            raise ValueError("operator token digest is invalid")
        self.path = Path(path)
        self.operator_token_digest = operator_token_digest
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.is_symlink():
            raise ApprovalRejected("approval database cannot be a symlink")
        self._initialize()

    @staticmethod
    def token_digest(token: bytes) -> str:
        return hashlib.sha256(token).hexdigest()

    def request(self, binding: ApprovalBindingV2) -> ApprovalEvent:
        return self._append(
            binding.request_id,
            event_type="REQUESTED",
            actor="agent-candidate",
            occurred_at=binding.requested_at,
            payload={"binding": binding.to_dict()},
            expected=None,
        )

    def decide(
        self,
        request_id: str,
        *,
        decision: ApprovalDecision,
        operator_token: bytes,
        occurred_at: datetime,
        reason: str,
    ) -> ApprovalEvent:
        self._require_operator(operator_token)
        event_type = "GRANTED" if decision is ApprovalDecision.GRANT else "DENIED"
        return self._append(
            request_id,
            event_type=event_type,
            actor="operator",
            occurred_at=occurred_at,
            payload={"reason": reason},
            expected=ApprovalState.REQUESTED,
        )

    def revoke(
        self,
        request_id: str,
        *,
        operator_token: bytes,
        occurred_at: datetime,
        reason: str,
    ) -> ApprovalEvent:
        self._require_operator(operator_token)
        return self._append(
            request_id,
            event_type="REVOKED",
            actor="operator",
            occurred_at=occurred_at,
            payload={"reason": reason},
            expected=ApprovalState.GRANTED,
        )

    def consume(
        self,
        request_id: str,
        *,
        binding: ApprovalBindingV2,
        checked_at: datetime,
    ) -> ApprovalEvent:
        stored = self.binding(request_id)
        if stored != binding:
            raise ApprovalRejected("approval binding does not match the request")
        if not stored.requested_at <= checked_at < stored.expires_at:
            self._expire(request_id, occurred_at=checked_at)
            raise ApprovalRejected("approval is expired or not yet valid")
        return self._append(
            request_id,
            event_type="CONSUMED",
            actor="supervisor",
            occurred_at=checked_at,
            payload={"binding_fingerprint": binding.fingerprint},
            expected=ApprovalState.GRANTED,
        )

    def state(
        self, request_id: str, *, checked_at: datetime | None = None
    ) -> ApprovalState:
        events = self.events(request_id)
        if not events:
            raise ApprovalRejected("approval request does not exist")
        state = ApprovalState(events[-1].event_type)
        if checked_at is not None and state is ApprovalState.GRANTED:
            binding = self.binding(request_id)
            if checked_at.tzinfo is None:
                raise ValueError("approval check time must be timezone-aware")
            if not binding.requested_at <= checked_at < binding.expires_at:
                return ApprovalState.EXPIRED
        return state

    def binding(self, request_id: str) -> ApprovalBindingV2:
        events = self.events(request_id)
        if not events or events[0].event_type != "REQUESTED":
            raise ApprovalRejected("approval request does not exist")
        try:
            return ApprovalBindingV2.from_dict(events[0].payload["binding"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ApprovalRejected("approval binding is corrupted") from exc

    def events(self, request_id: str | None = None) -> tuple[ApprovalEvent, ...]:
        with self._connect() as connection:
            query = "SELECT sequence, request_id, event_type, actor, occurred_at, payload, previous_hash, event_hash FROM approval_events"
            parameters: tuple[object, ...] = ()
            if request_id is not None:
                query += " WHERE request_id = ?"
                parameters = (request_id,)
            query += " ORDER BY sequence"
            rows = connection.execute(query, parameters).fetchall()
        events = tuple(
            ApprovalEvent(
                sequence=row[0],
                request_id=row[1],
                event_type=row[2],
                actor=row[3],
                occurred_at=datetime.fromisoformat(row[4]),
                payload=json.loads(row[5]),
                previous_hash=row[6],
                event_hash=row[7],
            )
            for row in rows
        )
        self._verify_chain(
            events if request_id is None else self.events()
        ) if request_id is not None else self._verify_chain(events)
        return events

    def export_summary(self) -> bytes:
        summaries = []
        request_ids = sorted({event.request_id for event in self.events()})
        for request_id in request_ids:
            binding = self.binding(request_id)
            summaries.append(
                {
                    "request_id": request_id,
                    "strategy_id": binding.strategy_id,
                    "binding_fingerprint": binding.fingerprint,
                    "state": self.state(request_id).value,
                    "expires_at": binding.expires_at.isoformat(),
                }
            )
        return canonical_json({"schema_version": 1, "approvals": summaries})

    def _expire(self, request_id: str, *, occurred_at: datetime) -> None:
        if self.state(request_id) is ApprovalState.GRANTED:
            self._append(
                request_id,
                event_type="EXPIRED",
                actor="supervisor",
                occurred_at=occurred_at,
                payload={"reason": "validity window elapsed"},
                expected=ApprovalState.GRANTED,
            )

    def _append(
        self,
        request_id: str,
        *,
        event_type: str,
        actor: str,
        occurred_at: datetime,
        payload: dict[str, Any],
        expected: ApprovalState | None,
    ) -> ApprovalEvent:
        if not request_id or occurred_at.tzinfo is None:
            raise ValueError("approval event identity or time is invalid")
        encoded_payload = canonical_json(payload).decode("utf-8")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise ApprovalRejected("approval database integrity check failed")
            all_rows = connection.execute(
                "SELECT sequence, request_id, event_type, actor, occurred_at, payload, previous_hash, event_hash FROM approval_events ORDER BY sequence"
            ).fetchall()
            existing = tuple(self._event_from_row(row) for row in all_rows)
            self._verify_chain(existing)
            request_events = [
                event for event in existing if event.request_id == request_id
            ]
            if expected is None:
                if request_events:
                    raise ApprovalRejected("approval request already exists")
            else:
                if (
                    not request_events
                    or ApprovalState(request_events[-1].event_type) is not expected
                ):
                    raise ApprovalRejected(
                        f"approval transition requires {expected.value}"
                    )
            previous_hash = existing[-1].event_hash if existing else "0" * 64
            sequence = (existing[-1].sequence + 1) if existing else 1
            material = canonical_json(
                {
                    "sequence": sequence,
                    "request_id": request_id,
                    "event_type": event_type,
                    "actor": actor,
                    "occurred_at": occurred_at.isoformat(),
                    "payload": payload,
                    "previous_hash": previous_hash,
                }
            )
            event_hash = hashlib.sha256(material).hexdigest()
            connection.execute(
                "INSERT INTO approval_events(sequence, request_id, event_type, actor, occurred_at, payload, previous_hash, event_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sequence,
                    request_id,
                    event_type,
                    actor,
                    occurred_at.isoformat(),
                    encoded_payload,
                    previous_hash,
                    event_hash,
                ),
            )
            connection.commit()
        return ApprovalEvent(
            sequence,
            request_id,
            event_type,
            actor,
            occurred_at,
            payload,
            previous_hash,
            event_hash,
        )

    def _verify_chain(self, events: tuple[ApprovalEvent, ...]) -> None:
        previous = "0" * 64
        for expected_sequence, event in enumerate(events, start=1):
            if event.sequence != expected_sequence or event.previous_hash != previous:
                raise ApprovalRejected("approval ledger chain is corrupted")
            material = canonical_json(
                {
                    "sequence": event.sequence,
                    "request_id": event.request_id,
                    "event_type": event.event_type,
                    "actor": event.actor,
                    "occurred_at": event.occurred_at.isoformat(),
                    "payload": event.payload,
                    "previous_hash": event.previous_hash,
                }
            )
            if not hmac.compare_digest(
                hashlib.sha256(material).hexdigest(), event.event_hash
            ):
                raise ApprovalRejected("approval ledger event hash is corrupted")
            previous = event.event_hash

    @staticmethod
    def _event_from_row(row: sqlite3.Row | tuple[Any, ...]) -> ApprovalEvent:
        return ApprovalEvent(
            sequence=row[0],
            request_id=row[1],
            event_type=row[2],
            actor=row[3],
            occurred_at=datetime.fromisoformat(row[4]),
            payload=json.loads(row[5]),
            previous_hash=row[6],
            event_hash=row[7],
        )

    def _require_operator(self, token: bytes) -> None:
        if not hmac.compare_digest(
            self.token_digest(token), self.operator_token_digest
        ):
            raise ApprovalRejected("operator authority is invalid")

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS approval_events (sequence INTEGER PRIMARY KEY, request_id TEXT NOT NULL, event_type TEXT NOT NULL, actor TEXT NOT NULL, occurred_at TEXT NOT NULL, payload TEXT NOT NULL, previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL UNIQUE)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS approval_request_idx ON approval_events(request_id, sequence)"
            )
            connection.commit()
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection
