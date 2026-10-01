"""Repository identity checks shared by explicitly approved effectful workers."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path


class SourceIdentityError(RuntimeError):
    """The running source tree cannot be bound to the approved commit."""


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("approval timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def verify_source_identity(commit_sha: str, repository: str | Path = ".") -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != commit_sha:
        raise SourceIdentityError("approval commit does not match HEAD")
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if dirty:
        raise SourceIdentityError("approved worker requires a clean working tree")
