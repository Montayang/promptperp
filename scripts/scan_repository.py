from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_EXACT = {".env"}
FORBIDDEN_PREFIXES = ("data/", "log/", "outputs/", "venv/", ".venv/")
FORBIDDEN_SUFFIXES = (".pem", ".key", ".log", ".sqlite3", ".db")
FORBIDDEN_PUBLIC_PREFIXES = (
    "src/promptperp/Clients/",
    "src/promptperp/Strategies/",
)
FORBIDDEN_PUBLIC_TOKENS = (
    "bian" + "bot",
    "monday_" + "original",
    "buffered_" + "relative_momentum",
    "br" + "m-production-",
)
EXPECTED_STRATEGY_MODULES = {
    "src/promptperp/strategies/__init__.py",
    "src/promptperp/strategies/base.py",
    "src/promptperp/strategies/integration.py",
    "src/promptperp/strategies/threshold_momentum.py",
}
CREDENTIAL = re.compile(
    r"""(?ix)
    \b(api[_-]?key|api[_-]?secret|email[_-]?password|smtp[_-]?password)
    \s*[:=]\s*["'][A-Za-z0-9_+/=-]{20,}["']
    """
)
PRIVATE_KEY_MARKER = "-----BEGIN " + "PRIVATE KEY-----"


def tracked_files() -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(item.decode("utf-8") for item in result.stdout.split(b"\0") if item)


def main() -> int:
    failures: list[tuple[str, int | None, str]] = []
    tracked = tracked_files()
    for relative in tracked:
        path = Path(relative)
        if (
            relative in FORBIDDEN_EXACT
            or relative.startswith(FORBIDDEN_PREFIXES)
            or path.suffix.lower() in FORBIDDEN_SUFFIXES
            or "__pycache__" in path.parts
            or ".egg-info" in relative
        ):
            failures.append((relative, None, "forbidden tracked artifact"))
            continue
        if relative.startswith(FORBIDDEN_PUBLIC_PREFIXES):
            failures.append((relative, None, "private compatibility path"))
            continue
        absolute = ROOT / path
        try:
            content = absolute.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(content.splitlines(), start=1):
            if PRIVATE_KEY_MARKER in line:
                failures.append((relative, number, "private key material"))
            if CREDENTIAL.search(line):
                failures.append((relative, number, "credential-like assignment"))
            if any(token in line.lower() for token in FORBIDDEN_PUBLIC_TOKENS):
                failures.append((relative, number, "private project marker"))
    strategy_modules = {
        item
        for item in tracked
        if item.startswith("src/promptperp/strategies/") and item.endswith(".py")
    }
    if strategy_modules != EXPECTED_STRATEGY_MODULES:
        failures.append(
            (
                "src/promptperp/strategies",
                None,
                "unexpected bundled strategy module set",
            )
        )
    if failures:
        for path, line, reason in failures:
            location = path if line is None else f"{path}:{line}"
            print(f"{location}: {reason}", file=sys.stderr)
        return 1
    print(f"repository scan passed: {len(tracked_files())} candidate files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
