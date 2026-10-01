from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from promptperp.signal_engine import EngineState, MarketEvent
from promptperp.strategy_spec import StrategySpec, canonical_json


class SandboxError(RuntimeError):
    pass


class TerminationReason(str, Enum):
    COMPLETED = "COMPLETED"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    PROTOCOL_REJECTED = "PROTOCOL_REJECTED"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: float = 3.0
    cpu_seconds: int = 2
    memory_bytes: int = 256 * 1024 * 1024
    output_bytes: int = 1024 * 1024
    input_bytes: int = 1024 * 1024
    open_files: int = 32
    processes: int = 8

    def __post_init__(self) -> None:
        if (
            min(
                self.timeout_seconds,
                self.cpu_seconds,
                self.memory_bytes,
                self.output_bytes,
                self.input_bytes,
                self.open_files,
                self.processes,
            )
            <= 0
        ):
            raise ValueError("sandbox limits must be positive")


@dataclass(frozen=True)
class SandboxCapability:
    available: bool
    backend: str
    reason: str


@dataclass(frozen=True)
class SandboxResult:
    reason: TerminationReason
    response: Mapping[str, Any] | None
    exit_code: int | None


class SandboxRunner:
    def __init__(self, *, limits: SandboxLimits | None = None):
        self.limits = limits or SandboxLimits()
        self.bwrap = shutil.which("bwrap")
        self.unshare = shutil.which("unshare")
        self.python = "/usr/bin/python3"

    def probe(self) -> SandboxCapability:
        if not self.bwrap or not self.unshare or not Path(self.python).is_file():
            return SandboxCapability(
                False, "bubblewrap+unshare", "required executable is unavailable"
            )
        command = [
            self.unshare,
            "--user",
            "--map-root-user",
            "--net",
            self.bwrap,
            *self._base_bwrap(),
            "--",
            self.python,
            "-I",
            "-S",
            "-c",
            "import os,socket; assert os.environ.get('HOME') == '/nonexistent'; assert not os.path.exists('/home'); assert not any(k.lower().endswith(('key','secret','password','token')) for k in os.environ); routes=open('/proc/net/route').read().splitlines()[1:]; assert not routes; blocked=False\ntry: socket.create_connection(('192.0.2.1',80),0.01)\nexcept OSError: blocked=True\nassert blocked; print('OK')",
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=self.limits.timeout_seconds,
                env={"PATH": "/usr/bin:/bin"},
                preexec_fn=self._limit_resources,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return SandboxCapability(False, "bubblewrap+unshare", type(exc).__name__)
        if completed.returncode != 0 or completed.stdout.strip() != b"OK":
            return SandboxCapability(
                False, "bubblewrap+unshare", "isolation probe failed"
            )
        return SandboxCapability(True, "bubblewrap+unshare", "available")

    def run(
        self,
        *,
        spec: StrategySpec,
        events: tuple[MarketEvent, ...],
        state: EngineState,
    ) -> SandboxResult:
        request = canonical_json(
            {
                "schema_version": 1,
                "spec": dict(spec.normalized),
                "events": [
                    {
                        "sequence": event.sequence,
                        "observed_at": event.observed_at.isoformat(),
                        "symbol": event.symbol,
                        "values": {
                            key: str(value) for key, value in event.values.items()
                        },
                    }
                    for event in events
                ],
                "state": state.to_dict(),
            }
        )
        if len(request) > self.limits.input_bytes:
            return SandboxResult(TerminationReason.RESOURCE_LIMIT, None, None)
        capability = self.probe()
        if not capability.available:
            return SandboxResult(TerminationReason.CAPABILITY_UNAVAILABLE, None, None)
        with tempfile.TemporaryDirectory(prefix="promptperp-sandbox-") as temporary:
            root = Path(temporary)
            stage = root / "app"
            input_root = root / "input"
            package = stage / "promptperp"
            stage.mkdir(mode=0o700)
            input_root.mkdir(mode=0o700)
            package.mkdir(mode=0o700)
            (package / "__init__.py").write_bytes(b"")
            source_root = Path(__file__).resolve().parents[1]
            shutil.copy2(Path(__file__).with_name("worker.py"), stage / "worker.py")
            for name in ("strategy_spec", "signal_engine"):
                shutil.copytree(
                    source_root / name,
                    package / name,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            request_path = input_root / "request.json"
            request_path.write_bytes(request)
            os.chmod(request_path, 0o400)
            command = [
                self.unshare or "unshare",
                "--user",
                "--map-root-user",
                "--net",
                self.bwrap or "bwrap",
                *self._base_bwrap(),
                "--ro-bind",
                str(stage),
                "/app",
                "--ro-bind",
                str(input_root),
                "/input",
                "--chdir",
                "/tmp",
                "--",
                self.python,
                "-I",
                "-S",
                "/app/worker.py",
            ]
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    timeout=self.limits.timeout_seconds,
                    env={"PATH": "/usr/bin:/bin"},
                    preexec_fn=self._limit_resources,
                )
            except subprocess.TimeoutExpired:
                return SandboxResult(TerminationReason.TIMEOUT, None, None)
            except OSError:
                return SandboxResult(
                    TerminationReason.CAPABILITY_UNAVAILABLE, None, None
                )
        if len(completed.stdout) > self.limits.output_bytes:
            return SandboxResult(
                TerminationReason.OUTPUT_LIMIT, None, completed.returncode
            )
        try:
            response = json.loads(completed.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return SandboxResult(
                TerminationReason.PROTOCOL_REJECTED, None, completed.returncode
            )
        if not isinstance(response, dict) or response.get("schema_version") != 1:
            return SandboxResult(
                TerminationReason.PROTOCOL_REJECTED, None, completed.returncode
            )
        if completed.returncode != 0 or response.get("status") != "OK":
            return SandboxResult(
                TerminationReason.PROTOCOL_REJECTED, response, completed.returncode
            )
        if set(response) != {"schema_version", "status", "proposals", "state"}:
            return SandboxResult(
                TerminationReason.PROTOCOL_REJECTED, None, completed.returncode
            )
        return SandboxResult(
            TerminationReason.COMPLETED, response, completed.returncode
        )

    def _base_bwrap(self) -> list[str]:
        # The outer unshare process already owns the user and network
        # namespaces. Creating a second user namespace inside bubblewrap is
        # redundant and is rejected by some otherwise-capable Linux hosts.
        command = [
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup-try",
            "--new-session",
            "--die-with-parent",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
        ]
        if Path("/lib64").exists():
            command.extend(("--ro-bind", "/lib64", "/lib64"))
        command.extend(
            (
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--clearenv",
                "--setenv",
                "PATH",
                "/usr/bin",
                "--setenv",
                "HOME",
                "/nonexistent",
            )
        )
        return command

    def _limit_resources(self) -> None:
        resource.setrlimit(
            resource.RLIMIT_CPU, (self.limits.cpu_seconds, self.limits.cpu_seconds)
        )
        resource.setrlimit(
            resource.RLIMIT_AS, (self.limits.memory_bytes, self.limits.memory_bytes)
        )
        resource.setrlimit(
            resource.RLIMIT_FSIZE, (self.limits.output_bytes, self.limits.output_bytes)
        )
        resource.setrlimit(
            resource.RLIMIT_NOFILE, (self.limits.open_files, self.limits.open_files)
        )
        resource.setrlimit(
            resource.RLIMIT_NPROC, (self.limits.processes, self.limits.processes)
        )
