from __future__ import annotations

import json

from promptperp.operations.platform_service import main


def write_config(path):
    path.write_text(
        """
[portfolio]
allocatable_equity = "1000"
reserve_fraction = "0.1"
allowed_symbols = ["BTCUSDT", "FFFUSDT"]
max_total_positions = 2
max_total_margin = "500"
balance_buffer = "100"
max_signal_age_seconds = "30"
max_account_age_seconds = "15"
max_leverage = 5
shared_symbol_policy = "EXCLUSIVE"

[[allocation]]
strategy_id = "threshold_momentum"
fraction = "0.8"
max_margin_per_trade = "100"
max_leverage = 2
max_loss_per_trade = "5"
max_positions = 1
""".strip()
        + "\n",
        encoding="utf-8",
    )


def invoke(tmp_path, *arguments):
    config = tmp_path / "platform.toml"
    if not config.exists():
        write_config(config)
    return main(
        [
            "--database",
            str(tmp_path / "platform.sqlite3"),
            "--config",
            str(config),
            *arguments,
        ]
    )


def test_cli_exposes_plugins_validate_plan_start_status_stop(tmp_path, capsys):
    assert invoke(tmp_path, "plugins") == 0
    plugins = json.loads(capsys.readouterr().out)
    assert {item["strategy_id"] for item in plugins} == {"threshold_momentum"}

    assert (
        invoke(
            tmp_path,
            "validate",
            "--strategy-id",
            "threshold_momentum",
            "--parameters",
            '{"symbol":"BTCUSDT","margin":"100"}',
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "VALID"
    assert (
        invoke(
            tmp_path,
            "plan",
            "--strategy-id",
            "threshold_momentum",
            "--run-id",
            "offline-run",
            "--parameters",
            '{"symbol":"BTCUSDT","margin":"100"}',
            "--at",
            "2026-10-01T00:00:00+00:00",
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "PLANNED"
    assert (
        invoke(
            tmp_path,
            "start",
            "--run-id",
            "offline-run",
            "--at",
            "2026-10-01T00:00:01+00:00",
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "RUNNING"
    assert invoke(tmp_path, "status", "--run-id", "offline-run") == 0
    assert json.loads(capsys.readouterr().out)["state"] == "RUNNING"
    assert (
        invoke(
            tmp_path,
            "stop",
            "--run-id",
            "offline-run",
            "--reason",
            "offline acceptance",
            "--at",
            "2026-10-01T00:00:02+00:00",
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "STOPPED"


def test_cli_reconcile_foreign_snapshot_returns_blocking_status(tmp_path, capsys):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "observed_at": "2026-10-01T00:00:00+00:00",
                "available_balance": "1000",
                "reconciliation_ok": True,
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "position_side": "LONG",
                        "quantity": "1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert (
        invoke(
            tmp_path,
            "reconcile",
            "--snapshot",
            str(snapshot),
            "--at",
            "2026-10-01T00:00:00+00:00",
        )
        == 2
    )
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "BLOCKED"
    assert "FOREIGN_POSITION_DETECTED" in output["reasons"]
