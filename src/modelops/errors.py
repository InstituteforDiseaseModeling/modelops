"""Error types for ModelOps."""


class ModelOpsError(Exception):
    """Base exception for ModelOps errors."""
    pass


class AuthError(ModelOpsError):
    """Authentication or authorization error."""
    pass


class ConfigError(ModelOpsError):
    """Configuration error."""
    pass


class InfrastructureError(ModelOpsError):
    """Infrastructure provisioning or management error."""
    pass