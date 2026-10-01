from __future__ import annotations


def pool_cash(pool_id: str) -> str:
    return f"asset:pool:{pool_id}:cash"


def investor_capital(investor_id: str) -> str:
    return f"equity:investor:{investor_id}:capital"


def investor_withdrawals(investor_id: str) -> str:
    return f"equity:investor:{investor_id}:withdrawals"


def realized_pnl(pool_id: str) -> str:
    return f"income:pool:{pool_id}:realized-pnl"


def funding(pool_id: str) -> str:
    return f"income:pool:{pool_id}:funding"


def trading_fees(pool_id: str) -> str:
    return f"expense:pool:{pool_id}:trading-fees"
