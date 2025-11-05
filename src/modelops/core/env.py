"""Environment resolution module - the single source of truth for environment names.

This module provides a consistent way to resolve environment names across the codebase,
eliminating the hardcoded restrictions and providing flexibility.
"""

import os

# CRITICAL: We don't restrict environment names anymore!
# Users can use ANY valid name they want: dev, staging, prod, test, vsb-dev, etc.
# The old VALID_ENVIRONMENTS restriction was causing unnecessary failures.
DEFAULT_ENVIRONMENT = "dev"


def resolve_env(env: str | None = None, required: bool = False) -> str:
    """Resolve environment name from various sources.

    Args:
        env: Explicit environment name
        required: If True, raise exception if no environment found

    Returns:
        Resolved environment name

    Raises:
        ValueError: If required=True and no environment could be resolved

    Resolution order:
    1. Explicit env parameter
    2. MODELOPS_ENV environment variable
    3. DEFAULT_ENVIRONMENT (dev)
    """
    # 1. Use explicit parameter if provided
    if env:
        return env

    # 2. Check environment variable
    from_env = os.environ.get("MODELOPS_ENV")
    if from_env:
        return from_env

    # 3. Use default
    if not required:
        return DEFAULT_ENVIRONMENT

    # 4. If required but not found, raise error
    raise ValueError("No environment specified. Set MODELOPS_ENV or provide --env parameter.")


def is_production(env: str) -> bool:
    """Check if environment is production.

    Args:
        env: Environment name

    Returns:
        True if production environment
    """
    return env.lower() in {"prod", "production"}


def is_development(env: str) -> bool:
    """Check if environment is development.

    Args:
        env: Environment name

    Returns:
        True if development environment
    """
    return env.lower() in {"dev", "development", "local"}


def validate_env_name(env: str) -> bool:
    """Validate environment name format.

    Args:
        env: Environment name to validate

    Returns:
        True if valid

    Rules:
    - Must be non-empty
    - Must start with letter
    - Can contain letters, numbers, hyphens
    - Max 20 characters (reasonable limit for resource names)
    """
    if not env or len(env) > 20:
        return False

    # Must start with letter
    if not env[0].isalpha():
        return False

    # Can only contain letters, numbers, hyphens
    for char in env:
        if not (char.isalnum() or char == "-"):
            return False

    return True


def sanitize_env_name(env: str) -> str:
    """Sanitize environment name for use in resource names.

    Args:
        env: Environment name

    Returns:
        Sanitized name suitable for Azure resources

    Examples:
        "dev" -> "dev"
        "vsb-dev" -> "vsbdev"
        "my_test" -> "mytest"
    """
    # Remove any non-alphanumeric characters
    sanitized = "".join(c for c in env.lower() if c.isalnum())

    # Ensure it starts with a letter
    if sanitized and not sanitized[0].isalpha():
        sanitized = "e" + sanitized  # Prefix with 'e' for environment

    # Truncate if too long
    return sanitized[:20]
