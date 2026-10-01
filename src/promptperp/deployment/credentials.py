from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

from promptperp.deployment.models import HostLayout, SafetyAttestation, canonical_json

_SERVICE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_ALLOWED = {
    "exchange": {"BINANCE_API_KEY", "BINANCE_API_SECRET"},
    "email-outbox": {
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "SMTP_FROM",
    },
    "email-query": {
        "IMAP_HOST",
        "IMAP_PORT",
        "IMAP_USER",
        "IMAP_PASSWORD",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "SMTP_FROM",
    },
}


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class CredentialReceipt:
    schema_version: int
    service: str
    generation: int
    key_names: tuple[str, ...]
    rotated_at: datetime

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or not _SERVICE.fullmatch(self.service)
            or self.generation <= 0
        ):
            raise ValueError("credential receipt identity is invalid")
        if self.rotated_at.tzinfo is None or not self.key_names:
            raise ValueError("credential receipt time or keys are invalid")

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["rotated_at"] = self.rotated_at.isoformat()
        return value


class CredentialStore:
    def __init__(self, layout: HostLayout):
        self.layout = layout

    def rotate(
        self,
        *,
        service: str,
        values: Mapping[str, str],
        rotated_at: datetime,
        attestation: SafetyAttestation | None = None,
    ) -> CredentialReceipt:
        if not _SERVICE.fullmatch(service) or service not in _ALLOWED:
            raise CredentialError("credential service is unsupported")
        if rotated_at.tzinfo is None:
            raise ValueError("credential rotation time must be timezone-aware")
        expected = _ALLOWED[service]
        if set(values) != expected or any(not _KEY.fullmatch(key) for key in values):
            raise CredentialError("credential key set is incomplete or excessive")
        if any(
            not isinstance(value, str)
            or not value
            or len(value) > 4096
            or "\n" in value
            or "\r" in value
            or "\x00" in value
            for value in values.values()
        ):
            raise CredentialError("credential value encoding is invalid")
        if service == "exchange" and (
            attestation is None
            or not attestation.safe_for_mutation(checked_at=rotated_at)
        ):
            raise CredentialError(
                "exchange credential rotation requires a fresh flat reconciled stop"
            )
        self.layout.credentials.mkdir(parents=True, exist_ok=True)
        os.chmod(self.layout.credentials, 0o700)
        previous = self.receipt(service, required=False)
        receipt = CredentialReceipt(
            schema_version=1,
            service=service,
            generation=1 if previous is None else previous.generation + 1,
            key_names=tuple(sorted(values)),
            rotated_at=rotated_at,
        )
        payload = "".join(f"{key}={values[key]}\n" for key in sorted(values)).encode()
        self._atomic_write(self.layout.credentials / f"{service}.env", payload)
        self._atomic_write(
            self.layout.credentials / f"{service}.receipt.json",
            canonical_json(receipt.to_dict()),
        )
        return receipt

    def receipt(
        self, service: str, *, required: bool = True
    ) -> CredentialReceipt | None:
        path = self.layout.credentials / f"{service}.receipt.json"
        if not path.exists():
            if required:
                raise CredentialError("credential receipt is missing")
            return None
        try:
            raw = json.loads(path.read_bytes())
            if canonical_json(raw) != path.read_bytes():
                raise ValueError
            return CredentialReceipt(
                schema_version=raw["schema_version"],
                service=raw["service"],
                generation=raw["generation"],
                key_names=tuple(raw["key_names"]),
                rotated_at=datetime.fromisoformat(raw["rotated_at"]),
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise CredentialError("credential receipt is corrupted") from exc

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}-", dir=path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if os.path.exists(temporary):
                os.unlink(temporary)
