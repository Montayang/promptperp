"""Caller-supplied strategy valuations and offline reports; no exchange access."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo


class EquityHistory:
    def __init__(
        self,
        path: Path,
        deployment_id: str,
        *,
        initial_equity: Decimal,
        schedule_timezone: str = "UTC",
        schedule_hour: int = 0,
    ):
        if not deployment_id or not initial_equity.is_finite() or initial_equity <= 0:
            raise ValueError("deployment identity and positive initial equity required")
        if type(schedule_hour) is not int or not 0 <= schedule_hour <= 23:
            raise ValueError("daily hour must be 0 through 23")
        self.initial_equity = initial_equity
        self.schedule_timezone = ZoneInfo(schedule_timezone)
        self.schedule_hour = schedule_hour
        self.path, self.deployment_id = path, deployment_id
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS samples (deployment TEXT, minute TEXT, payload TEXT NOT NULL, PRIMARY KEY(deployment, minute))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS deliveries (deployment TEXT, day TEXT, state TEXT NOT NULL, PRIMARY KEY(deployment, day))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS report_policy (deployment TEXT PRIMARY KEY, policy TEXT NOT NULL)"
            )
            policy = json.dumps(
                [str(initial_equity.normalize()), schedule_timezone, schedule_hour]
            )
            db.execute(
                "INSERT OR IGNORE INTO report_policy VALUES (?, ?)",
                (deployment_id, policy),
            )
            if (
                db.execute(
                    "SELECT policy FROM report_policy WHERE deployment=?",
                    (deployment_id,),
                ).fetchone()[0]
                != policy
            ):
                raise ValueError("report policy differs from existing deployment")
        path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def record(
        self,
        *,
        at: datetime,
        equity: Decimal,
        realized: Decimal,
        commission: Decimal,
        funding: Decimal,
        gross: Decimal,
        net: Decimal,
        position_count: int,
    ) -> None:
        if at.tzinfo is None or not all(
            v.is_finite() for v in (equity, realized, commission, funding, gross, net)
        ):
            raise ValueError("invalid strategy valuation")
        minute = (
            at.astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()
        )
        payload = {
            "at": at.astimezone(timezone.utc).isoformat(),
            "equity": str(equity),
            "realized_pnl": str(realized),
            "commission": str(commission),
            "funding": str(funding),
            "unrealized_pnl": str(
                equity - self.initial_equity - realized + commission - funding
            ),
            "gross": str(gross),
            "net": str(net),
            "position_count": position_count,
        }
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO samples VALUES (?, ?, ?)",
                (self.deployment_id, minute, json.dumps(payload)),
            )

    def samples(self, start: datetime, end: datetime) -> list[dict[str, str | int]]:
        if start.tzinfo is None or end.tzinfo is None or start > end:
            raise ValueError("report requires ordered timezone-aware bounds")
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM samples WHERE deployment=? AND minute>=? AND minute<=? ORDER BY minute",
                (
                    self.deployment_id,
                    start.astimezone(timezone.utc)
                    .replace(second=0, microsecond=0)
                    .isoformat(),
                    end.astimezone(timezone.utc)
                    .replace(second=0, microsecond=0)
                    .isoformat(),
                ),
            ).fetchall()
        return [
            p
            for (raw,) in rows
            if start <= datetime.fromisoformat(str((p := json.loads(raw))["at"])) <= end
        ]

    def summary(
        self, start: datetime, end: datetime
    ) -> tuple[str, list[dict[str, str | int]]]:
        rows = self.samples(start, end)
        if not rows:
            return (
                "No valid samples in this interval; absence of data is not a zero return.",
                rows,
            )
        first, last = Decimal(str(rows[0]["equity"])), Decimal(str(rows[-1]["equity"]))
        peak = first
        drawdown = Decimal("0")
        for row in rows:
            value = Decimal(str(row["equity"]))
            peak = max(peak, value)
            if peak > 0:
                drawdown = max(drawdown, (peak - value) / peak)
        period_return = "N/A" if first <= 0 else f"{(last / first - 1) * 100:.4f}%"
        tail = rows[-1]
        body = (
            f"Strategy {self.deployment_id}\nSample coverage: {rows[0]['at']} to {tail['at']} ({len(rows)} points)\n"
            f"Equity: {last:.8f} USDT; sampled interval PnL: {last - first:.8f} USDT ({period_return})\n"
            f"Since inception: {last - self.initial_equity:.8f} USDT ({(last / self.initial_equity - 1) * 100:.4f}%); sampled max drawdown: {drawdown * 100:.4f}%\n"
            f"Realized PnL: {tail['realized_pnl']}; unrealized PnL: {tail['unrealized_pnl']}\n"
            f"Cumulative fees: {tail['commission']}; funding: {tail['funding']}\n"
            f"Positions: {tail['position_count']}; gross/net notional: {tail['gross']} / {tail['net']} USDT\n"
            "Strategy economic equity, not investor pool NAV. No gap filling. Caller must supply fresh reconciled valuations. Deposits/withdrawals are not supported within a deployment."
        )
        return body, rows

    def daily(self, now: datetime, notify: Callable[[str, str], None]) -> bool:
        if now.tzinfo is None:
            raise ValueError("daily scheduling requires timezone-aware time")
        local = now.astimezone(self.schedule_timezone)
        due = local.replace(hour=self.schedule_hour, minute=0, second=0, microsecond=0)
        if local < due:
            return False
        day = local.date().isoformat()
        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM deliveries WHERE deployment=? AND day=?",
                (self.deployment_id, day),
            ).fetchone():
                return False
        body, rows = self.summary(due - timedelta(days=1), now)
        if not rows or now - datetime.fromisoformat(str(rows[-1]["at"])) > timedelta(
            minutes=2
        ):
            return False
        # Claim before SMTP: a timeout is uncertain, never automatically duplicate.
        with self.connect() as db:
            claimed = db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?, 'SENDING')",
                (self.deployment_id, day),
            ).rowcount
        if not claimed:
            return False
        try:
            notify("Strategy daily summary " + day, body)
        except Exception:
            with self.connect() as db:
                db.execute(
                    "UPDATE deliveries SET state='UNCERTAIN' WHERE deployment=? AND day=?",
                    (self.deployment_id, day),
                )
            raise
        with self.connect() as db:
            db.execute(
                "UPDATE deliveries SET state='SENT' WHERE deployment=? AND day=?",
                (self.deployment_id, day),
            )
        return True

    def html(self, start: datetime, end: datetime) -> str:
        body, rows = self.summary(start, end)
        lines = []
        if rows:
            values = [float(Decimal(str(r["equity"]))) for r in rows]
            times = [datetime.fromisoformat(str(r["at"])).timestamp() for r in rows]
            low, high = min(values), max(values)

            def point(i: int) -> str:
                return f"{40 + 720 * (times[i] - times[0]) / max(1, times[-1] - times[0]):.2f},{260 - 220 * (values[i] - low) / max(0.00000001, high - low):.2f}"

            for i in range(1, len(rows)):
                if times[i] - times[i - 1] <= 120:
                    lines.append(
                        f'<polyline points="{point(i - 1)} {point(i)}" fill="none" stroke="blue"/>'
                    )
            lines.append(
                f'<text x="5" y="20">Equity USDT: {low:.4f} – {high:.4f}; time UTC →</text>'
            )
        return (
            '<!doctype html><meta charset="utf-8"><title>Strategy report</title><h1>Strategy equity curve</h1><svg viewBox="0 0 800 300">'
            + "".join(lines)
            + "</svg><pre>"
            + escape(body)
            + "</pre>"
        )
