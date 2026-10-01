from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from promptperp.execution.models import ExecutionEvent


class LedgerCorrupted(RuntimeError):
    """The append-only execution ledger cannot be trusted."""


class EventLedger:
    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(
        self,
        *,
        strategy_id: str,
        run_id: str,
        intent_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        occurred_at: datetime | None = None,
    ) -> ExecutionEvent:
        events = self.load()
        sequence = len(events) + 1
        previous_hash = events[-1].record_hash if events else ""
        timestamp = occurred_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ValueError("event timestamp must be timezone-aware")
        base = {
            "schema_version": self.SCHEMA_VERSION,
            "sequence": sequence,
            "occurred_at": timestamp.isoformat(),
            "strategy_id": strategy_id,
            "run_id": run_id,
            "intent_id": intent_id,
            "event_type": event_type,
            "payload": self._json_value(dict(payload)),
            "previous_hash": previous_hash,
        }
        record_hash = self._hash(base)
        record = {**base, "record_hash": record_hash}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            encoded = (
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            written = 0
            while written < len(encoded):
                written += os.write(descriptor, encoded[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return self._event(record)

    def load(self) -> tuple[ExecutionEvent, ...]:
        if not self.path.exists():
            return ()
        events: list[ExecutionEvent] = []
        previous_hash = ""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise LedgerCorrupted("execution ledger cannot be read") from exc
        for expected_sequence, line in enumerate(lines, start=1):
            if not line:
                raise LedgerCorrupted("execution ledger contains an empty record")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerCorrupted("execution ledger contains invalid JSON") from exc
            if record.get("schema_version") != self.SCHEMA_VERSION:
                raise LedgerCorrupted("execution ledger schema is unsupported")
            if record.get("sequence") != expected_sequence:
                raise LedgerCorrupted("execution ledger sequence is invalid")
            if record.get("previous_hash") != previous_hash:
                raise LedgerCorrupted("execution ledger hash chain is broken")
            claimed_hash = record.get("record_hash")
            base = {key: value for key, value in record.items() if key != "record_hash"}
            if not isinstance(claimed_hash, str) or claimed_hash != self._hash(base):
                raise LedgerCorrupted("execution ledger record was modified")
            event = self._event(record)
            events.append(event)
            previous_hash = event.record_hash
        return tuple(events)

    @staticmethod
    def _hash(value: Mapping[str, Any]) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()

    @classmethod
    def _json_value(cls, value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {str(key): cls._json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json_value(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        raise TypeError(f"unsupported event payload type: {type(value).__name__}")

    @staticmethod
    def _event(record: Mapping[str, Any]) -> ExecutionEvent:
        try:
            occurred_at = datetime.fromisoformat(str(record["occurred_at"]))
            if occurred_at.tzinfo is None:
                raise ValueError
            payload = record["payload"]
            if not isinstance(payload, dict):
                raise TypeError
            return ExecutionEvent(
                schema_version=int(record["schema_version"]),
                sequence=int(record["sequence"]),
                occurred_at=occurred_at,
                strategy_id=str(record["strategy_id"]),
                run_id=str(record["run_id"]),
                intent_id=str(record["intent_id"]),
                event_type=str(record["event_type"]),
                payload=payload,
                previous_hash=str(record["previous_hash"]),
                record_hash=str(record["record_hash"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerCorrupted("execution ledger record shape is invalid") from exc
