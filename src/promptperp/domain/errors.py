class ExchangeError(RuntimeError):
    """Base class for normalized exchange failures."""


class RequestRejected(ExchangeError):
    """The exchange explicitly rejected a request."""


class RequestUnknown(ExchangeError):
    """The request outcome or account state cannot be determined safely."""


class ResponseShapeError(RequestUnknown):
    """The exchange response cannot be converted to the domain model."""


class ReconciliationFailed(ExchangeError):
    """Local intent and exchange state cannot be reconciled."""


class ProtectionFailed(ExchangeError):
    """Required exchange-hosted protection cannot be confirmed."""


class ForeignPositionDetected(ExchangeError):
    """A position cannot be proven to belong to the current strategy run."""


class ForeignOrderDetected(ExchangeError):
    """An open order cannot be proven to belong to the current strategy run."""


class RiskRejected(ExchangeError):
    """The operator risk policy rejected a trading action."""
