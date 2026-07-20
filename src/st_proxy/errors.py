class BrokerError(RuntimeError):
    """Base error for controlled broker failures."""


class ConfigurationError(BrokerError):
    """Raised when configuration is unsafe or invalid."""


class UpstreamError(BrokerError):
    """Raised when an upstream service fails."""


class HandoffError(BrokerError):
    """Raised when a GPU handoff cannot complete safely."""


class ChatUnavailable(BrokerError):
    """Raised when the LLM is not in a verified usable state."""
