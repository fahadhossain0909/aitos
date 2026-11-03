"""AITOS exception hierarchy. All custom exceptions inherit from AITOSError
so callers can catch broadly (``except AITOSError``) or specifically."""


class AITOSError(Exception):
    """Base class for all AITOS exceptions."""


class ModuleNotInitializedError(AITOSError):
    """Raised when a module method is called before ``initialize()``."""


class EventSchemaValidationError(AITOSError):
    """Raised when an event fails schema validation before publish."""


class GovernanceViolationError(AITOSError):
    """Raised when an action is rejected by the governance/approval gate."""


class AgentNotRegisteredError(AITOSError):
    """Raised when an operation references an unregistered agent."""


class DecisionFusionError(AITOSError):
    """Raised when the Decision Fusion Engine cannot produce a decision."""


class CircuitBreakerTrippedError(AITOSError):
    """Raised when a safety circuit breaker blocks an operation."""


class TradeNotFoundError(AITOSError):
    """Raised when an operation references an unknown/closed trade_id."""
