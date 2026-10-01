from __future__ import annotations

import re
from hashlib import sha256

_ALLOWED_ROLE = re.compile(r"^[a-z0-9_-]+$")


def client_order_id(
    *,
    strategy_id: str,
    run_id: str,
    intent_id: str,
    role: str,
) -> str:
    """Build a deterministic Binance-compatible ID without exposing raw identifiers."""

    if not all((strategy_id, run_id, intent_id, role)):
        raise ValueError("client order id inputs are required")
    normalized_role = role.lower()
    if not _ALLOWED_ROLE.fullmatch(normalized_role):
        raise ValueError("client order role contains unsupported characters")
    digest = sha256(
        f"{strategy_id}\0{run_id}\0{intent_id}\0{normalized_role}".encode()
    ).hexdigest()[:24]
    return f"bb-{normalized_role[:7]}-{digest}"
