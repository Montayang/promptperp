from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from promptperp.deployment.manager import DeploymentError, DeploymentManager
from promptperp.deployment.models import canonical_json

_WHEEL = re.compile(r"[A-Za-z0-9_.+-]+\.whl")
_HEX64 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class WheelhouseLock:
    schema_version: int
    application_sha256: str
    wheels: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not _HEX64.fullmatch(self.application_sha256):
            raise ValueError("wheelhouse lock identity is invalid")
        if any(
            not _WHEEL.fullmatch(name) or not _HEX64.fullmatch(digest)
            for name, digest in self.wheels.items()
        ):
            raise ValueError("wheelhouse lock entry is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "application_sha256": self.application_sha256,
            "wheels": dict(sorted(self.wheels.items())),
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict())).hexdigest()


@dataclass(frozen=True)
class InstallationReceipt:
    schema_version: int
    release_fingerprint: str
    wheelhouse_fingerprint: str
    python_executable: str
    installed_at: datetime

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or len(self.release_fingerprint) != 64
            or len(self.wheelhouse_fingerprint) != 64
            or not self.python_executable
            or self.installed_at.tzinfo is None
        ):
            raise ValueError("installation receipt is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "release_fingerprint": self.release_fingerprint,
            "wheelhouse_fingerprint": self.wheelhouse_fingerprint,
            "python_executable": self.python_executable,
            "installed_at": self.installed_at.isoformat(),
        }


class OfflineInstaller:
    def __init__(self, manager: DeploymentManager):
        self.manager = manager

    def install(
        self,
        *,
        release_fingerprint: str,
        wheelhouse: str | Path,
        python_executable: str | Path,
        installed_at: datetime,
    ) -> InstallationReceipt:
        if installed_at.tzinfo is None:
            raise ValueError("installation time must be timezone-aware")
        manifest = self.manager.release_manifest(release_fingerprint)
        source = Path(wheelhouse)
        if not source.is_dir() or source.is_symlink():
            raise DeploymentError("wheelhouse is missing or unsafe")
        if {item.name for item in source.iterdir()} != {"wheelhouse.json", "wheels"}:
            raise DeploymentError("wheelhouse root has unexpected files")
        lock = self._load_lock(source / "wheelhouse.json")
        wheels_root = source / "wheels"
        actual = (
            {path.name for path in wheels_root.iterdir()}
            if wheels_root.is_dir() and not wheels_root.is_symlink()
            else set()
        )
        if actual != set(lock.wheels):
            raise DeploymentError("wheelhouse has missing or extra dependency wheels")
        if lock.application_sha256 != manifest.artifact_sha256:
            raise DeploymentError("wheelhouse does not bind the application artifact")
        for name, expected in lock.wheels.items():
            path = wheels_root / name
            if (
                not path.is_file()
                or path.is_symlink()
                or self._digest(path) != expected
            ):
                raise DeploymentError("wheelhouse dependency digest mismatch")
        try:
            python = Path(python_executable).resolve(strict=True)
        except OSError as exc:
            raise DeploymentError("installation Python is missing or unsafe") from exc
        if not python.is_file():
            raise DeploymentError("installation Python is missing or unsafe")
        detected_version = (
            self._run(
                (
                    str(python),
                    "-I",
                    "-c",
                    "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
                )
            )
            .decode()
            .strip()
        )
        if detected_version != manifest.python_version:
            raise DeploymentError("installation Python does not match the release")
        release_root = self.manager.layout.releases / release_fingerprint
        runtime = release_root / "runtime"
        if runtime.exists():
            raise DeploymentError("release runtime is already installed")
        temporary = Path(tempfile.mkdtemp(prefix=".runtime-", dir=release_root))
        copied = temporary / "wheelhouse"
        copied.mkdir()
        try:
            for name in sorted(lock.wheels):
                shutil.copyfile(wheels_root / name, copied / name)
                os.chmod(copied / name, 0o444)
            venv = temporary / ".venv"
            self._run((str(python), "-m", "venv", str(venv)))
            pip = venv / "bin/pip"
            app = release_root / manifest.artifact_name
            self._run(
                (
                    str(pip),
                    "install",
                    "--no-index",
                    "--disable-pip-version-check",
                    "--find-links",
                    str(copied),
                    str(app),
                )
            )
            self._run((str(pip), "check"))
            self._run((str(venv / "bin/python"), "-I", "-c", "import promptperp"))
            receipt = InstallationReceipt(
                schema_version=1,
                release_fingerprint=release_fingerprint,
                wheelhouse_fingerprint=lock.fingerprint,
                python_executable=str(python),
                installed_at=installed_at,
            )
            (temporary / "installation.json").write_bytes(
                canonical_json(receipt.to_dict())
            )
            os.chmod(temporary / "installation.json", 0o444)
            os.rename(temporary, runtime)
            return receipt
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    @staticmethod
    def _load_lock(path: Path) -> WheelhouseLock:
        if not path.is_file() or path.is_symlink():
            raise DeploymentError("wheelhouse lock is missing or unsafe")
        try:
            payload = path.read_bytes()
            raw = json.loads(payload)
            if canonical_json(raw) != payload or set(raw) != {
                "schema_version",
                "application_sha256",
                "wheels",
            }:
                raise ValueError
            return WheelhouseLock(
                schema_version=raw["schema_version"],
                application_sha256=raw["application_sha256"],
                wheels=dict(raw["wheels"]),
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise DeploymentError("wheelhouse lock is malformed") from exc

    @staticmethod
    def _run(command: tuple[str, ...]) -> bytes:
        unshare = shutil.which("unshare", path="/usr/bin:/bin")
        if unshare is None:
            raise DeploymentError("network namespace capability is unavailable")
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "PIP_CONFIG_FILE": "/dev/null",
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        completed = subprocess.run(
            (unshare, "--user", "--map-root-user", "--net", *command),
            check=False,
            capture_output=True,
            timeout=180,
            env=environment,
        )
        if completed.returncode != 0:
            raise DeploymentError("offline installation command failed")
        return completed.stdout

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
