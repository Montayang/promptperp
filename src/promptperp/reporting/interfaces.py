from __future__ import annotations

from typing import Protocol

from promptperp.accounting.models import InvestorView


class InvestorViewReader(Protocol):
    def investor_view_for_email(self, email: str) -> InvestorView | None:
        """Return only the verified sender's latest reconciled view."""
