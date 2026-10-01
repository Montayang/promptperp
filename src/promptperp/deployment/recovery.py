from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from promptperp.deployment.models import HostLayout, SafetyAttestation, canonical_json

_MAX_FILES = 10000
_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


class RecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArchiveReceipt:
    archive: Path
    sha256: str
    files: int
    total_bytes: int
    created_at: datetime


class StateArchive:
    def __init__(self, layout: HostLayout):
        self.layout = layout

    def create(
        self,
        destination: str | Path,
        *,
        attestation: SafetyAttestation,
        created_at: datetime,
    ) -> ArchiveReceipt:
        if created_at.tzinfo is None:
            raise ValueError("archive creation time must be timezone-aware")
        if not attestation.safe_for_mutation(checked_at=created_at):
            raise RecoveryError("state archive requires a fresh flat reconciled stop")
        if not self.layout.state.is_dir() or self.layout.state.is_symlink():
            raise RecoveryError("deployment state root is missing or unsafe")
        if (self.layout.status / "deployment-transaction.json").exists():
            raise RecoveryError(
                "state archive refuses an uncertain deployment transaction"
            )
        files: list[tuple[str, Path, int, str]] = []
        total = 0
        for path in sorted(self.layout.state.rglob("*")):
            if path.is_symlink():
                raise RecoveryError("state archive refuses symbolic links")
            if not path.is_file():
                continue
            relative = path.relative_to(self.layout.state).as_posix()
            if relative.endswith((".lease", ".lock", ".sock")):
                continue
            size = path.stat().st_size
            total += size
            if len(files) >= _MAX_FILES or total > _MAX_TOTAL_BYTES:
                raise RecoveryError("state archive exceeds resource limits")
            files.append((relative, path, size, self._digest(path)))
        manifest = {
            "schema_version": 1,
            "created_at": created_at.isoformat(),
            "attestation_observed_at": attestation.observed_at.isoformat(),
            "files": [
                {"path": relative, "size": size, "sha256": digest}
                for relative, _, size, digest in files
            ],
        }
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".state-archive-", dir=target.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr("manifest.json", canonical_json(manifest))
                for relative, path, _, _ in files:
                    archive.write(path, f"state/{relative}")
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return ArchiveReceipt(
            target, self._digest(target), len(files), total, created_at
        )

    def restore(
        self,
        archive_path: str | Path,
        target_state: str | Path,
        *,
        attestation: SafetyAttestation,
        restored_at: datetime,
    ) -> ArchiveReceipt:
        if restored_at.tzinfo is None:
            raise ValueError("archive restore time must be timezone-aware")
        if not attestation.safe_for_mutation(checked_at=restored_at):
            raise RecoveryError("state restore requires a fresh flat reconciled stop")
        source = Path(archive_path)
        if not source.is_file() or source.is_symlink():
            raise RecoveryError("state archive is missing or unsafe")
        destination = Path(target_state)
        if destination.is_symlink() or (
            destination.exists() and any(destination.iterdir())
        ):
            raise RecoveryError("restore target must be empty")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}-restore-", dir=destination.parent
            )
        )
        archive_digest = self._digest(source)
        try:
            with zipfile.ZipFile(source) as archive:
                names = archive.namelist()
                if names.count("manifest.json") != 1 or len(names) > _MAX_FILES + 1:
                    raise RecoveryError("state archive member list is invalid")
                manifest_bytes = archive.read("manifest.json")
                manifest = json.loads(manifest_bytes)
                if (
                    canonical_json(manifest) != manifest_bytes
                    or manifest.get("schema_version") != 1
                ):
                    raise RecoveryError("state archive manifest is invalid")
                declared = manifest.get("files")
                if not isinstance(declared, list):
                    raise RecoveryError("state archive file manifest is invalid")
                expected_names = {"manifest.json"}
                total = 0
                entries: list[tuple[str, int, str]] = []
                for item in declared:
                    if not isinstance(item, dict) or set(item) != {
                        "path",
                        "size",
                        "sha256",
                    }:
                        raise RecoveryError("state archive file entry is invalid")
                    relative, size, digest = item["path"], item["size"], item["sha256"]
                    if not isinstance(relative, str) or not self._safe_relative(
                        relative
                    ):
                        raise RecoveryError("state archive path traversal was rejected")
                    if (
                        not isinstance(size, int)
                        or size < 0
                        or not isinstance(digest, str)
                        or len(digest) != 64
                    ):
                        raise RecoveryError("state archive file metadata is invalid")
                    expected_names.add(f"state/{relative}")
                    total += size
                    entries.append((relative, size, digest))
                if set(names) != expected_names or total > _MAX_TOTAL_BYTES:
                    raise RecoveryError("state archive contents differ from manifest")
                for relative, size, digest in entries:
                    payload = archive.read(f"state/{relative}")
                    if (
                        len(payload) != size
                        or hashlib.sha256(payload).hexdigest() != digest
                    ):
                        raise RecoveryError("state archive payload digest mismatch")
                    target = temporary / relative
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    self._write_new(target, payload)
        except (
            OSError,
            zipfile.BadZipFile,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ) as exc:
            shutil.rmtree(temporary)
            raise RecoveryError("state archive cannot be trusted") from exc
        except Exception:
            shutil.rmtree(temporary)
            raise
        os.chmod(temporary, 0o700)
        try:
            if destination.exists():
                destination.rmdir()
            os.replace(temporary, destination)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return ArchiveReceipt(source, archive_digest, len(entries), total, restored_at)

    @staticmethod
    def _safe_relative(value: str) -> bool:
        path = PurePosixPath(value)
        return (
            bool(value)
            and not path.is_absolute()
            and ".." not in path.parts
            and str(path) == value
        )

    @staticmethod
    def _write_new(path: Path, payload: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
