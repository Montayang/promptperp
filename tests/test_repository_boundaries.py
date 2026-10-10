from __future__ import annotations

import ast
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"


def test_source_does_not_reference_bianbt():
    references = []
    for path in SOURCE_ROOT.rglob("*.py"):
        if "bianbt" in path.read_text(encoding="utf-8").lower():
            references.append(path.relative_to(REPOSITORY_ROOT))
    assert references == []


def test_no_module_level_dotenv_load_calls():
    offenders = []
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for statement in tree.body:
            if not isinstance(statement, ast.Expr):
                continue
            call = statement.value
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                if call.func.id == "load_dotenv":
                    offenders.append(path.relative_to(REPOSITORY_ROOT))
    assert offenders == []


def test_runtime_and_secret_files_are_not_tracked():
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    tracked = {item.decode("utf-8") for item in completed.stdout.split(b"\0") if item}
    forbidden_exact = {".env"}
    forbidden_prefixes = ("data/", "log/", "outputs/", ".venv/", "venv/")

    assert tracked.isdisjoint(forbidden_exact)
    assert not any(path.startswith(forbidden_prefixes) for path in tracked)


def test_safety_documents_exist():
    assert (REPOSITORY_ROOT / "docs" / "ENTRYPOINT_SUPPORT.md").is_file()
    assert (REPOSITORY_ROOT / "docs" / "TESTING_SAFETY.md").is_file()


def test_user_documentation_is_bilingual_and_cross_linked():
    pairs = (
        ("README.md", "README.zh-CN.md"),
        ("CHANGELOG.md", "CHANGELOG.zh-CN.md"),
        ("docs/README.md", "docs/README.zh-CN.md"),
        ("docs/BEGINNER_GUIDE.md", "docs/BEGINNER_GUIDE.zh-CN.md"),
        ("docs/SHARED_EXECUTION.md", "docs/SHARED_EXECUTION.zh-CN.md"),
        ("docs/EXECUTION_RELIABILITY.md", "docs/EXECUTION_RELIABILITY.zh-CN.md"),
        ("SECURITY.md", "SECURITY.zh-CN.md"),
        ("CONTRIBUTING.md", "CONTRIBUTING.zh-CN.md"),
    )

    for english_name, chinese_name in pairs:
        english_path = REPOSITORY_ROOT / english_name
        chinese_path = REPOSITORY_ROOT / chinese_name
        assert english_path.is_file(), english_name
        assert chinese_path.is_file(), chinese_name

        english_header = "\n".join(
            english_path.read_text(encoding="utf-8").splitlines()[:8]
        )
        chinese_header = "\n".join(
            chinese_path.read_text(encoding="utf-8").splitlines()[:8]
        )
        assert Path(chinese_name).name in english_header, english_name
        assert Path(english_name).name in chinese_header, chinese_name


def test_private_strategy_modules_are_absent():
    forbidden = (
        SOURCE_ROOT / "promptperp" / "Clients",
        SOURCE_ROOT / "promptperp" / "Strategies",
        SOURCE_ROOT / "promptperp" / "operations" / "protocol_acceptance.py",
    )

    assert not any(path.exists() for path in forbidden)


def test_multi_strategy_runtime_has_no_exchange_imports():
    offenders = []
    forbidden = ("promptperp.exchange",)
    for path in (SOURCE_ROOT / "promptperp" / "runtime").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.startswith(forbidden) for name in names):
                module = next(name for name in names if name.startswith(forbidden))
            if module:
                offenders.append((path.relative_to(REPOSITORY_ROOT), module))
    assert offenders == []


def test_agent_policy_modules_cannot_import_effectful_runtime_layers():
    offenders = []
    roots = (
        "strategy_spec",
        "signal_engine",
        "evaluation",
        "strategy_packages",
        "approvals",
        "sandbox",
        "agent_pipeline",
    )
    forbidden = (
        "promptperp.accounting",
        "promptperp.config",
        "promptperp.exchange",
        "promptperp.execution",
        "promptperp.notifications",
        "promptperp.operations",
        "promptperp.reporting",
        "promptperp.risk",
        "promptperp.runtime",
    )
    for root in roots:
        for path in (SOURCE_ROOT / "promptperp" / root).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                matches = [
                    name
                    for name in names
                    if any(name.startswith(item) for item in forbidden)
                ]
                offenders.extend(
                    (path.relative_to(REPOSITORY_ROOT), name) for name in matches
                )
    assert offenders == []


def test_deployment_control_plane_has_no_exchange_or_notification_imports():
    offenders = []
    forbidden = (
        "promptperp.exchange",
        "promptperp.execution",
        "promptperp.notifications",
        "promptperp.risk",
    )
    paths = list((SOURCE_ROOT / "promptperp" / "deployment").rglob("*.py"))
    paths.append(SOURCE_ROOT / "promptperp" / "operations" / "deployment_service.py")
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            offenders.extend(
                (path.relative_to(REPOSITORY_ROOT), name)
                for name in names
                if any(name.startswith(item) for item in forbidden)
            )
    assert offenders == []
