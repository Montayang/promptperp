from __future__ import annotations

import hashlib
from datetime import datetime

from promptperp.accounting.models import InvestorView
from promptperp.reporting.models import InvestorStatement, ReportPeriod


def build_statement(
    view: InvestorView, *, period: ReportPeriod, created_at: datetime
) -> InvestorStatement:
    identity = "|".join(
        (view.investor_id, period.start_at.isoformat(), period.end_at.isoformat())
    )
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return InvestorStatement(
        statement_id=f"statement-{suffix}",
        investor_id=view.investor_id,
        display_name=view.display_name,
        pool_id=view.pool_id,
        strategy_id=view.strategy_id,
        strategy_version=view.strategy_version,
        period=period,
        equity=view.equity,
        net_contributions=view.net_contributions,
        cumulative_return_rate=view.return_rate,
        unit_nav=view.unit_nav,
        snapshot_at=view.snapshot_at,
        reconciliation_id=view.reconciliation_id,
        created_at=created_at,
    )


def render_statement_text(statement: InvestorStatement) -> str:
    rate = statement.cumulative_return_rate * 100
    return "\n".join(
        (
            f"{statement.display_name} 资金报告",
            f"报告周期：{statement.period.start_at.isoformat()} 至 {statement.period.end_at.isoformat()}",
            f"当前策略：{statement.strategy_id} ({statement.strategy_version})",
            f"当前净值：{statement.equity:.8f} USDT",
            f"净入金：{statement.net_contributions:.8f} USDT",
            f"累计回报率：{rate:.4f}%",
            f"单位净值：{statement.unit_nav}",
            f"净值截止：{statement.snapshot_at.isoformat()}",
            f"对账编号：{statement.reconciliation_id}",
            "本邮件仅供只读查询，不能用于下单、出入金或修改策略。",
        )
    )
