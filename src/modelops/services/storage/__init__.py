"""Storage backends for ModelOps simulation cache."""

import logging
import os
from typing import Optional

from .azure import AzureBlobBackend
from .base import StorageBackend
from .local import LocalFileBackend

logger = logging.getLogger(__name__)


def get_cloud_backend(container: str = "cache") -> StorageBackend:
    """Auto-detect cloud provider and return appropriate backend.

    This integrates with infrastructure provisioned by Pulumi:
    - Azure: Uses BlobStorage component outputs via connection string
    - Future: AWS support may be added
    - GCP: Will use GCSStorage component outputs (future)

    Args:
        container: Container/bucket name (default: "cache")

    Returns:
        Appropriate storage backend based on environment

    Examples:
        >>> # After Azure provisioning
        >>> backend = get_cloud_backend("simcache")
        >>> backend.save("test/key", b"data")
    """
    # Azure detection (matches your BlobStorage component)
    if os.environ.get("AZURE_STORAGE_CONNECTION_STRING"):
        logger.info(f"Detected Azure environment, using container: {container}")
        return AzureBlobBackend(container=container)

    # AWS detection (future)
    # if os.environ.get("AWS_ACCESS_KEY_ID"):
    # Future: AWS support may be added here

    # GCP detection (future)
    # if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    #     logger.info(f"Detected GCP environment, using bucket: {container}")
    #     return GCSBackend(bucket=container)

    # Fallback to local
    logger.info("No cloud environment detected, using local storage")
    return LocalFileBackend(base_path=f"/tmp/modelops/{container}")


def get_default_backend() -> StorageBackend:
    """Get default backend based on environment.

    Uses "cache" as the default container name.

    Returns:
        Storage backend for simulation cache
    """
    return get_cloud_backend("cache")


def get_backend(backend_type: str = "auto", **kwargs) -> StorageBackend:
    """Factory function to get specific storage backend.

    Args:
        backend_type: One of "auto", "azure", "local"
        **kwargs: Backend-specific configuration

    Returns:
        Storage backend instance

    Raises:
        ValueError: If backend_type is unknown

    Examples:
        >>> # Explicit Azure backend
        >>> backend = get_backend("azure", container="results")

        >>> # Explicit local backend
        >>> backend = get_backend("local", base_path="/tmp/cache")

        >>> # Auto-detect
        >>> backend = get_backend("auto")
    """
    if backend_type == "auto":
        return get_cloud_backend(**kwargs)
    elif backend_type == "azure":
        return AzureBlobBackend(**kwargs)
    elif backend_type == "local":
        return LocalFileBackend(**kwargs)
    else:
        raise ValueError(f"Unknown backend type: {backend_type}")


__all__ = [
    "StorageBackend",
    "AzureBlobBackend",
    "LocalFileBackend",
    "get_cloud_backend",
    "get_default_backend",
    "get_backend",
]
