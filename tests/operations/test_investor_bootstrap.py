from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from promptperp.operations.investor_bootstrap import main
from promptperp.storage import SQLiteAccountingStore


def private_example(tmp_path):
    source = Path(__file__).parents[2] / "configs/investor_bootstrap.example.toml"
    target = tmp_path / "investor_bootstrap.toml"
    shutil.copyfile(source, target)
    target.chmod(0o600)
    return target


def test_dry_run_validates_without_creating_database(tmp_path, capsys):
    config = private_example(tmp_path)
    database = tmp_path / "accounting.sqlite3"

    assert main(["--config", str(config), "--database", str(database)]) == 0

    assert not database.exists()
    output = capsys.readouterr().out
    assert "bootstrap_config_valid=true" in output
    assert "database_changed=false" in output
    assert "Example Owner" not in output


def test_config_permissions_are_required_even_for_dry_run(tmp_path):
    config = private_example(tmp_path)
    config.chmod(0o640)

    with pytest.raises(PermissionError, match="owner-only"):
        main(["--config", str(config)])


def test_apply_creates_auditable_ledger_without_printing_identity(tmp_path, capsys):
    config = private_example(tmp_path)
    database = tmp_path / "accounting.sqlite3"

    assert (
        main(
            [
                "--config",
                str(config),
                "--database",
                str(database),
                "--apply",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "bootstrap_applied=true" in output
    assert "Example Owner" not in output
    store = SQLiteAccountingStore(database)
    store.integrity_check()
    assert store.audit_ledger() == 1
