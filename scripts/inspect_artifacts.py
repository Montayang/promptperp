from __future__ import annotations

import re
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
CREDENTIAL = re.compile(
    rb"""(?ix)
    \b(api[_-]?key|api[_-]?secret|email[_-]?password|smtp[_-]?password)
    \s*[:=]\s*["'][A-Za-z0-9_+/=-]{20,}["']
    """
)
PRIVATE_KEY_MARKER = b"-----BEGIN " + b"PRIVATE KEY-----"
FORBIDDEN_PARTS = {
    "Clients",
    "Strategies",
    "data",
    "log",
    "outputs",
    ".env",
    "__pycache__",
}
PRIVATE_MARKERS = (
    b"bian" + b"bot",
    b"monday_" + b"original",
    b"buffered_" + b"relative_momentum",
    b"br" + b"m-production-",
)


def inspect_member(name: str, content: bytes) -> None:
    parts = set(Path(name).parts)
    if parts & FORBIDDEN_PARTS:
        raise RuntimeError(f"artifact contains forbidden path: {name}")
    if PRIVATE_KEY_MARKER in content or CREDENTIAL.search(content):
        raise RuntimeError(f"artifact contains credential-like material: {name}")
    if any(marker in content.lower() for marker in PRIVATE_MARKERS):
        raise RuntimeError(f"artifact contains private project marker: {name}")


def main() -> None:
    artifacts = sorted(DIST.glob("*"))
    if not artifacts:
        raise RuntimeError("no distribution artifacts found")
    for artifact in artifacts:
        if artifact.suffix == ".whl":
            with zipfile.ZipFile(artifact) as archive:
                for name in archive.namelist():
                    inspect_member(name, archive.read(name))
        elif artifact.name.endswith(".tar.gz"):
            with tarfile.open(artifact, "r:gz") as archive:
                for member in archive.getmembers():
                    if member.isfile():
                        handle = archive.extractfile(member)
                        assert handle is not None
                        inspect_member(member.name, handle.read())
        else:
            raise RuntimeError(f"unexpected distribution artifact: {artifact.name}")
    print(f"artifact inspection passed: {len(artifacts)} files")


if __name__ == "__main__":
    main()
