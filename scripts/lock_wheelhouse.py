from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from promptperp.deployment.installer import WheelhouseLock
from promptperp.deployment.models import canonical_json


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a canonical hash lock for an already downloaded offline wheelhouse."
    )
    parser.add_argument("--application", required=True)
    parser.add_argument("--wheels-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    application = Path(args.application)
    wheels = Path(args.wheels_dir)
    if not application.is_file() or application.is_symlink():
        raise RuntimeError("application wheel is missing or unsafe")
    if not wheels.is_dir() or wheels.is_symlink():
        raise RuntimeError("dependency wheel directory is missing or unsafe")
    entries = {}
    for path in sorted(wheels.iterdir()):
        if not path.is_file() or path.is_symlink() or path.suffix != ".whl":
            raise RuntimeError("wheelhouse may contain only regular wheel files")
        entries[path.name] = _digest(path)
    lock = WheelhouseLock(
        schema_version=1,
        application_sha256=_digest(application),
        wheels=entries,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_json(lock.to_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
