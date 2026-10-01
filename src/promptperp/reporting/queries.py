from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from promptperp.reporting.interfaces import InvestorViewReader


class QueryCommand(str, Enum):
    NAV = "净值"
    RETURN = "收益"
    STRATEGY = "策略"
    HELP = "帮助"


class InboxRegistry(Protocol):
    def accept(self, *, message_id: str, sender: str, received_at: datetime) -> bool:
        """Return true exactly once while enforcing the sender rate limit."""


@dataclass(frozen=True)
class InboundQuery:
    message_id: str
    sender: str
    subject: str
    body: str
    received_at: datetime
    has_attachments: bool = False


class EmailQueryHandler:
    def __init__(
        self,
        accounting: InvestorViewReader,
        registry: InboxRegistry,
    ):
        self.accounting = accounting
        self.registry = registry

    def handle(self, query: InboundQuery) -> str | None:
        if not self.registry.accept(
            message_id=query.message_id,
            sender=query.sender,
            received_at=query.received_at,
        ):
            return None
        if query.has_attachments:
            return "无法处理该请求。"
        command = self._command(query.subject, query.body)
        if command is None:
            return "无法处理该请求。"
        view = self.accounting.investor_view_for_email(query.sender)
        if view is None:
            return "无法处理该请求。"
        if command is QueryCommand.HELP:
            return "可用只读命令：净值、收益、策略、帮助。"
        if command is QueryCommand.NAV:
            return (
                f"当前净值：{view.equity:.8f} USDT\n"
                f"截止时间：{view.snapshot_at.isoformat()}\n"
                f"对账编号：{view.reconciliation_id}"
            )
        if command is QueryCommand.RETURN:
            return (
                f"累计回报率：{view.return_rate * 100:.4f}%\n"
                f"当前净值：{view.equity:.8f} USDT\n"
                f"截止时间：{view.snapshot_at.isoformat()}"
            )
        return (
            f"当前策略：{view.strategy_id} ({view.strategy_version})\n"
            f"截止时间：{view.snapshot_at.isoformat()}"
        )

    @staticmethod
    def _command(subject: str, body: str) -> QueryCommand | None:
        candidates = [value.strip() for value in (subject, body) if value.strip()]
        if len(candidates) != 1:
            return None
        try:
            return QueryCommand(candidates[0])
        except ValueError:
            return None
