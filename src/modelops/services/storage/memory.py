"""In-memory implementation of VersionedStore for testing.

This provides a thread-safe, in-memory implementation that mimics
the CAS semantics of cloud storage providers.
"""

import threading

from .versioned import VersionToken


class InMemoryVersionedStore:
    """In-memory versioned storage for testing.

    This implementation is thread-safe and provides the same CAS
    semantics as cloud storage, making it perfect for unit tests.
    """

    def __init__(self):
        """Initialize empty store with thread safety."""
        self._data: dict[str, bytes] = {}
        self._versions: dict[str, int] = {}
        self._version_counter = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[bytes, VersionToken] | None:
        """Get current value and version."""
        with self._lock:
            if key not in self._data:
                return None
            return (self._data[key], VersionToken(self._versions[key]))

    def put(self, key: str, value: bytes, version: VersionToken) -> bool:
        """Update if version matches (CAS)."""
        with self._lock:
            # Key must exist for put
            if key not in self._data:
                return False

            # Check version match
            current_version = self._versions[key]
            if current_version != version.value:
                return False  # Version mismatch

            # Update with new version
            self._version_counter += 1
            self._data[key] = value
            self._versions[key] = self._version_counter
            return True

    def create_if_absent(self, key: str, value: bytes) -> bool:
        """Create only if doesn't exist."""
        with self._lock:
            if key in self._data:
                return False  # Already exists

            # Create with initial version
            self._version_counter += 1
            self._data[key] = value
            self._versions[key] = self._version_counter
            return True

    def list_keys(self, prefix: str = "") -> list[str]:
        """List keys with prefix."""
        with self._lock:
            return [key for key in self._data.keys() if key.startswith(prefix)]

    def delete(self, key: str) -> bool:
        """Delete a key."""
        with self._lock:
            if key not in self._data:
                return False

            del self._data[key]
            del self._versions[key]
            return True

    def clear(self) -> None:
        """Clear all data (useful for tests)."""
        with self._lock:
            self._data.clear()
            self._versions.clear()
            self._version_counter = 0
