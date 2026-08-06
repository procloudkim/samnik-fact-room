class BackendError(Exception):
    """Base class for expected backend failures."""


class ProviderUnavailable(BackendError):
    """An external model or disclosure provider is unavailable."""


class RepositoryUnavailable(BackendError):
    """The persistence or retrieval layer is unavailable."""


class EvidenceValidationError(BackendError):
    """A result attempted to escape without sufficient evidence."""
