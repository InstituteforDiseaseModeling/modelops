"""Versioned storage protocol for optimistic concurrency control.

This module provides a cloud-agnostic interface for storage with
Compare-And-Swap (CAS) semantics, enabling safe concurrent updates
without locks or leases.
"""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class VersionToken:
    """Opaque version identifier for CAS operations.

    The actual value depends on the storage backend:
    - Azure: ETag string
    - GCS: metageneration number
    - DynamoDB: version counter
    """

    value: Any

    def __str__(self) -> str:
        return str(self.value)


class VersionedStore(Protocol):
    """Cloud-agnostic versioned storage with CAS semantics.

    This protocol defines the minimal interface needed for optimistic
    concurrency control. Implementations handle provider-specific details.

    All methods work with bytes to avoid JSON serialization quirks.
    JSON encoding/decoding happens at the JobRegistry layer.
    """

    def get(self, key: str) -> tuple[bytes, VersionToken] | None:
        """Get current value and version.

        Args:
            key: Storage key (e.g., "jobs/123/state.json")

        Returns:
            Tuple of (data bytes, version token) if exists, None otherwise.
            The version token is required for subsequent updates.
        """
        ...

    def put(self, key: str, value: bytes, version: VersionToken) -> bool:
        """Update value if version matches (Compare-And-Swap).

        Args:
            key: Storage key
            value: New value as bytes
            version: Expected current version

        Returns:
            True if update succeeded, False if version mismatch (retry needed).

        Note:
            Returns False instead of raising because conflicts are expected
            in concurrent scenarios and should trigger retries.
        """
        ...

    def create_if_absent(self, key: str, value: bytes) -> bool:
        """Create entry only if it doesn't exist.

        Args:
            key: Storage key
            value: Initial value as bytes

        Returns:
            True if created, False if already exists.

        Note:
            This is atomic - prevents duplicate job registration.
        """
        ...

    def list_keys(self, prefix: str = "") -> list[str]:
        """List all keys with given prefix.

        Args:
            prefix: Key prefix to filter (e.g., "jobs/")

        Returns:
            List of matching keys.

        Note:
            Used for listing jobs when index is unavailable.
        """
        ...

    def delete(self, key: str) -> bool:
        """Delete a key.

        Args:
            key: Storage key to delete

        Returns:
            True if deleted, False if didn't exist.
        """
        ...


class TooManyRetriesError(Exception):
    """Raised when CAS retries are exhausted."""

    pass
