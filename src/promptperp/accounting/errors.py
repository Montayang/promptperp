class AccountingError(RuntimeError):
    """Base class for fail-closed investor-accounting failures."""


class AccountingValidationError(AccountingError):
    pass


class DuplicateEventConflict(AccountingError):
    pass


class CashFlowBlocked(AccountingError):
    pass


class ReconciliationBlocked(AccountingError):
    pass


class LedgerIntegrityError(AccountingError):
    pass
