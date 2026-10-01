from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from promptperp.deployment.manager import DeploymentError, DeploymentManager
from promptperp.deployment.models import (
    HostLayout,
    PromotionEnvironment,
    canonical_json,
)

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_METRIC = re.compile(r"[a-z][a-z0-9_]{0,63}")


class HealthLevel(str, Enum):
    NORMAL = "NORMAL"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ComponentHealth:
    schema_version: int
    component: str
    level: HealthLevel
    observed_at: datetime
    reasons: tuple[str, ...]
    counters: Mapping[str, int]

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not _NAME.fullmatch(self.component):
            raise ValueError("component health identity is invalid")
        if self.observed_at.tzinfo is None:
            raise ValueError("component health time must be timezone-aware")
        if self.level is HealthLevel.NORMAL and self.reasons:
            raise ValueError("normal component cannot have health reasons")
        if self.level is not HealthLevel.NORMAL and not self.reasons:
            raise ValueError("non-normal component requires a reason")
        sensitive = (
            "key",
            "secret",
            "password",
            "token",
            "balance",
            "email",
            "account",
        )
        if any(
            not _METRIC.fullmatch(key)
            or any(fragment in key for fragment in sensitive)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in self.counters.items()
        ):
            raise ValueError("component counters are invalid or sensitive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "component": self.component,
            "level": self.level.value,
            "observed_at": self.observed_at.isoformat(),
            "reasons": list(self.reasons),
            "counters": dict(sorted(self.counters.items())),
        }


@dataclass(frozen=True)
class DeploymentHealth:
    schema_version: int
    level: HealthLevel
    observed_at: datetime
    releases: Mapping[str, str]
    components: tuple[ComponentHealth, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "level": self.level.value,
            "observed_at": self.observed_at.isoformat(),
            "releases": dict(sorted(self.releases.items())),
            "components": [component.to_dict() for component in self.components],
            "reasons": list(self.reasons),
        }


class DeploymentMonitor:
    def __init__(self, layout: HostLayout, manager: DeploymentManager):
        self.layout = layout
        self.manager = manager

    def publish_component(self, health: ComponentHealth) -> None:
        root = self.layout.status / "components"
        root.mkdir(parents=True, exist_ok=True)
        self._atomic_write(
            root / f"{health.component}.json", canonical_json(health.to_dict())
        )

    def collect(
        self, *, observed_at: datetime, stale_after_seconds: int = 300
    ) -> DeploymentHealth:
        if observed_at.tzinfo is None or stale_after_seconds <= 0:
            raise ValueError("monitor time or stale threshold is invalid")
        releases: dict[str, str] = {}
        reasons: list[str] = []
        if (self.layout.status / "deployment-transaction.json").exists():
            reasons.append("DEPLOYMENT_TRANSACTION_UNCERTAIN")
        try:
            self.manager.verify_history()
        except DeploymentError:
            reasons.append("DEPLOYMENT_HISTORY_CORRUPTED")
        for environment in PromotionEnvironment:
            try:
                state = self.manager.state(environment, required=False)
            except DeploymentError:
                reasons.append(f"{environment.value}_DEPLOYMENT_CORRUPTED")
                continue
            if state is not None:
                releases[environment.value] = state.active_release
        components: list[ComponentHealth] = []
        root = self.layout.status / "components"
        if root.exists():
            for path in sorted(root.glob("*.json")):
                try:
                    component = self._load_component(path)
                except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                    reasons.append(f"COMPONENT_STATUS_CORRUPTED:{path.stem}")
                    continue
                age = (observed_at - component.observed_at).total_seconds()
                if age < 0 or age > stale_after_seconds:
                    component = ComponentHealth(
                        schema_version=1,
                        component=component.component,
                        level=HealthLevel.BLOCKED,
                        observed_at=component.observed_at,
                        reasons=("STATUS_STALE",),
                        counters=component.counters,
                    )
                components.append(component)
        if (
            any(component.level is HealthLevel.BLOCKED for component in components)
            or reasons
        ):
            level = HealthLevel.BLOCKED
        elif any(component.level is HealthLevel.DEGRADED for component in components):
            level = HealthLevel.DEGRADED
        else:
            level = HealthLevel.NORMAL
        health = DeploymentHealth(
            schema_version=1,
            level=level,
            observed_at=observed_at,
            releases=releases,
            components=tuple(components),
            reasons=tuple(reasons),
        )
        self._atomic_write(
            self.layout.status / "health.json", canonical_json(health.to_dict())
        )
        self._atomic_write(
            self.layout.status / "metrics.prom", self._prometheus(health).encode()
        )
        self._atomic_write(
            self.layout.status / "dashboard.html", self._dashboard(health).encode()
        )
        if level is not HealthLevel.NORMAL:
            self._queue_alert(health)
        return health

    @staticmethod
    def _load_component(path: Path) -> ComponentHealth:
        if path.is_symlink():
            raise ValueError("linked component status is forbidden")
        payload = path.read_bytes()
        raw = json.loads(payload)
        if canonical_json(raw) != payload or set(raw) != {
            "schema_version",
            "component",
            "level",
            "observed_at",
            "reasons",
            "counters",
        }:
            raise ValueError("component status is not canonical")
        return ComponentHealth(
            schema_version=raw["schema_version"],
            component=raw["component"],
            level=HealthLevel(raw["level"]),
            observed_at=datetime.fromisoformat(raw["observed_at"]),
            reasons=tuple(raw["reasons"]),
            counters=dict(raw["counters"]),
        )

    @staticmethod
    def _prometheus(health: DeploymentHealth) -> str:
        levels = {
            HealthLevel.NORMAL: 0,
            HealthLevel.DEGRADED: 1,
            HealthLevel.BLOCKED: 2,
        }
        lines = [
            "# HELP promptperp_deployment_health Deployment health: 0 normal, 1 degraded, 2 blocked.",
            "# TYPE promptperp_deployment_health gauge",
            f"promptperp_deployment_health {levels[health.level]}",
        ]
        for component in health.components:
            lines.append(
                f'promptperp_component_health{{component="{component.component}"}} {levels[component.level]}'
            )
            for name, value in sorted(component.counters.items()):
                lines.append(
                    f'promptperp_component_{name}{{component="{component.component}"}} {value}'
                )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _dashboard(health: DeploymentHealth) -> str:
        rows = "".join(
            f"<tr><td>{html.escape(item.component)}</td><td>{item.level.value}</td><td>{html.escape(', '.join(item.reasons) or '-')}</td></tr>"
            for item in health.components
        )
        return (
            "<!doctype html><meta charset=utf-8><title>PromptPerp health</title>"
            f"<h1>{health.level.value}</h1><p>Observed {health.observed_at.isoformat()}</p>"
            "<table><thead><tr><th>Component</th><th>Health</th><th>Reasons</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )

    def _queue_alert(self, health: DeploymentHealth) -> None:
        fingerprint = hashlib.sha256(
            canonical_json(
                {
                    "level": health.level.value,
                    "reasons": health.reasons,
                    "components": [
                        (item.component, item.level.value, item.reasons)
                        for item in health.components
                    ],
                }
            )
        ).hexdigest()
        marker = self.layout.alerts / "last-fingerprint"
        if marker.exists() and marker.read_text() == fingerprint:
            return
        event = {
            "schema_version": 1,
            "fingerprint": fingerprint,
            "severity": health.level.value,
            "observed_at": health.observed_at.isoformat(),
            "reasons": list(health.reasons),
            "components": [
                {
                    "component": item.component,
                    "level": item.level.value,
                    "reasons": list(item.reasons),
                }
                for item in health.components
                if item.level is not HealthLevel.NORMAL
            ],
        }
        self.layout.alerts.mkdir(parents=True, exist_ok=True)
        path = self.layout.alerts / "outbox.ndjson"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, canonical_json(event) + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._atomic_write(marker, fingerprint.encode())

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
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
