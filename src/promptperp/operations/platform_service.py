"""Offline control plane for multi-strategy lifecycle; never accesses an exchange."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from promptperp.domain import PositionSide
from promptperp.runtime import (
    AccountOrder,
    AccountPosition,
    AccountSnapshot,
    MultiStrategyPlatform,
    PlatformMode,
    PortfolioGate,
    SQLitePlatformStore,
    StrategyPluginRegistry,
    load_platform_configuration,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plugins")
    validate = commands.add_parser("validate")
    validate.add_argument("--strategy-id", required=True)
    validate.add_argument("--parameters", default="{}")
    plan = commands.add_parser("plan")
    plan.add_argument("--strategy-id", required=True)
    plan.add_argument("--run-id", required=True)
    plan.add_argument("--parameters", default="{}")
    plan.add_argument("--at")
    start = commands.add_parser("start")
    start.add_argument("--run-id", required=True)
    start.add_argument("--at")
    rebalance = commands.add_parser("rebalance")
    rebalance.add_argument("--run-id", required=True)
    rebalance.add_argument("--at")
    status = commands.add_parser("status")
    status.add_argument("--run-id", required=True)
    stop = commands.add_parser("stop")
    stop.add_argument("--run-id", required=True)
    stop.add_argument("--reason", required=True)
    stop.add_argument("--at")
    mode = commands.add_parser("mode")
    mode.add_argument(
        "--set", required=True, choices=[item.value for item in PlatformMode]
    )
    mode.add_argument("--reason", required=True)
    mode.add_argument("--at")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--snapshot", required=True, type=Path)
    reconcile.add_argument("--at")
    report = commands.add_parser("report")
    report.add_argument("--run-id", required=True)
    report.add_argument("--at")
    return parser


def _time(raw: str | None) -> datetime:
    value = datetime.now(timezone.utc) if raw is None else datetime.fromisoformat(raw)
    if value.tzinfo is None:
        raise ValueError("operation timestamp must be timezone-aware")
    return value


def _parameters(raw: str) -> Mapping[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("strategy parameters must be a JSON object")
    return value


def _snapshot(path: Path) -> AccountSnapshot:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("account snapshot must be a JSON object")
    if not isinstance(raw.get("reconciliation_ok"), bool):
        raise ValueError("account snapshot reconciliation_ok must be boolean")
    for order in raw.get("open_orders", []):
        if not isinstance(order.get("reduce_only"), bool):
            raise ValueError("account order reduce_only must be boolean")
    return AccountSnapshot(
        observed_at=datetime.fromisoformat(str(raw["observed_at"])),
        available_balance=Decimal(str(raw["available_balance"])),
        reconciliation_ok=raw["reconciliation_ok"],
        positions=tuple(
            AccountPosition(
                symbol=str(item["symbol"]),
                position_side=PositionSide(str(item["position_side"])),
                quantity=Decimal(str(item["quantity"])),
                owner_strategy_id=item.get("owner_strategy_id"),
                owner_run_id=item.get("owner_run_id"),
            )
            for item in raw.get("positions", [])
        ),
        open_orders=tuple(
            AccountOrder(
                symbol=str(item["symbol"]),
                reduce_only=item["reduce_only"],
                owner_strategy_id=item.get("owner_strategy_id"),
                owner_run_id=item.get("owner_run_id"),
            )
            for item in raw.get("open_orders", [])
        ),
    )


def _platform(args: argparse.Namespace) -> MultiStrategyPlatform:
    config = load_platform_configuration(args.config)
    platform = MultiStrategyPlatform(
        store=SQLitePlatformStore(args.database),
        plugins=StrategyPluginRegistry(),
        allocation=config.allocation,
        portfolio_gate=PortfolioGate(config.portfolio),
    )
    platform.initialize()
    return platform


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    platform = _platform(args)
    if args.command == "plugins":
        print(
            json.dumps(
                [
                    {
                        "strategy_id": item.strategy_id,
                        "strategy_version": item.strategy_version,
                        "interface_version": item.interface_version,
                        "event_kind": item.event_kind,
                        "fingerprint": item.fingerprint,
                    }
                    for item in platform.plugins.discover()
                ],
                sort_keys=True,
            )
        )
        return 0
    if args.command == "validate":
        normalized = platform.validate(args.strategy_id, _parameters(args.parameters))
        print(
            json.dumps(
                {"status": "VALID", "parameters": dict(normalized)}, sort_keys=True
            )
        )
        return 0
    if args.command == "plan":
        plan = platform.plan(
            strategy_id=args.strategy_id,
            run_id=args.run_id,
            raw_parameters=_parameters(args.parameters),
            created_at=_time(args.at),
        )
        print(
            json.dumps(
                {
                    "status": "PLANNED",
                    "strategy_id": plan.strategy_id,
                    "run_id": plan.run_id,
                    "capital_budget": str(plan.capital_budget),
                    "max_margin_per_trade": str(plan.max_margin_per_trade),
                    "max_leverage": plan.max_leverage,
                    "max_loss_per_trade": str(plan.max_loss_per_trade),
                    "max_positions": plan.max_positions,
                    "plugin_fingerprint": plan.plugin_fingerprint,
                    "parameter_fingerprint": plan.parameter_fingerprint,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "start":
        status = platform.start(args.run_id, occurred_at=_time(args.at))
        print(json.dumps(platform.public_status(status), sort_keys=True))
        return 0
    if args.command == "rebalance":
        status = platform.rebalance(args.run_id, occurred_at=_time(args.at))
        print(json.dumps(platform.public_status(status), sort_keys=True))
        return 0
    if args.command == "status":
        print(
            json.dumps(
                platform.public_status(platform.status(args.run_id)), sort_keys=True
            )
        )
        return 0
    if args.command == "stop":
        status = platform.stop(
            args.run_id, reason=args.reason, occurred_at=_time(args.at)
        )
        print(json.dumps(platform.public_status(status), sort_keys=True))
        return 0
    if args.command == "mode":
        selected = PlatformMode(args.set)
        platform.set_mode(selected, reason=args.reason, occurred_at=_time(args.at))
        print(json.dumps({"mode": selected.value}, sort_keys=True))
        return 0
    if args.command == "reconcile":
        reasons = platform.reconcile(
            _snapshot(args.snapshot), occurred_at=_time(args.at)
        )
        print(
            json.dumps(
                {"status": "PASSED" if not reasons else "BLOCKED", "reasons": reasons},
                sort_keys=True,
            )
        )
        return 0 if not reasons else 2
    if args.command == "report":
        report = platform.report(args.run_id, generated_at=_time(args.at))
        values = asdict(report)
        values["generated_at"] = report.generated_at.isoformat()
        for key in (
            "realized_pnl",
            "commission",
            "funding",
            "maximum_slippage_bps",
        ):
            values[key] = str(values[key])
        print(json.dumps(values, sort_keys=True))
        return 0
    raise ValueError("unsupported platform command")


if __name__ == "__main__":
    raise SystemExit(main())
